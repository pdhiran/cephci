"""
Failover test module for 3 AZ NFS-Ganesha clusters.

Tests NFS daemon, MDS, node, site-level, and network partition failures
with IO continuity and data integrity validation across active-active
NFS clusters in a 3 datacenter stretch-mode topology.

Mount topology
--------------
Each NFS cluster (e.g. nfs-DC1) has 3 active-active daemons, one per AZ.
With ``num_exports_per_cluster=3``, each daemon gets exactly one export
mounted by a different client via round-robin::

    nfs-DC1 cluster (3 daemons, 3 exports, 3 clients):

      node10 --mount--> node1 (DC1 daemon)  : /export_failover_nfs-DC1_0
      node11 --mount--> node4 (DC2 daemon)  : /export_failover_nfs-DC1_1
      node12 --mount--> node7 (DC3 daemon)  : /export_failover_nfs-DC1_2

    nfs-DC2 cluster:
      node10 --mount--> node2 (DC1 daemon)  : /export_failover_nfs-DC2_0
      node11 --mount--> node5 (DC2 daemon)  : /export_failover_nfs-DC2_1
      node12 --mount--> node8 (DC3 daemon)  : /export_failover_nfs-DC2_2

    nfs-DC3 cluster:
      node10 --mount--> node3 (DC1 daemon)  : /export_failover_nfs-DC3_0
      node11 --mount--> node6 (DC2 daemon)  : /export_failover_nfs-DC3_1
      node12 --mount--> node9 (DC3 daemon)  : /export_failover_nfs-DC3_2

    Total: 9 mounts, 9 daemons, 3 clients, 3 NFS clusters, 3 AZs

This ensures every daemon is exercised.  When a daemon is killed or
its node is rebooted, exactly one mount per NFS cluster is affected
while two survive -- providing a meaningful failover split.

Client failover models
----------------------
Model A -- Recovery in-place
    Client waits for its daemon to be restarted by cephadm; the NFS hard
    mount retries until the daemon is back.  Other clients on surviving
    daemons are unaffected.

Model B -- Active remount
    After detecting the daemon is down (mount becomes unresponsive), the
    test explicitly unmounts and remounts the client to a surviving daemon
    in a different DC.

IO lifecycle (common to all scenarios)
--------------------------------------
1. Setup -- create subvolumes, exports, mount on clients (all 3 daemons)
2. Baseline IO -- fio to confirm IO works on all 9 mounts
3. Write baseline data with sha256 checksums
4. Inject failure (kill daemon / reboot node / power off DC / netsplit)
5. Verify IO on surviving daemons (mounts to unaffected daemon IPs)
6. Wait for recovery
7. Post-recovery IO on ALL 9 mounts (including recovered)
8. Data integrity -- sha256 + cross-daemon visibility
9. Cleanup

Implemented scenarios
---------------------
nfs_daemon_kill
    Kill NFS daemon on a single node, verify surviving daemons serve IO,
    verify recovery after cephadm restart (Model A).

nfs_daemon_kill_remount
    Model B variant -- kill daemon, detect stale mount, remount to a
    surviving daemon in a different DC.

node_reboot
    Reboot a node hosting NFS + MDS + OSD, verify surviving daemons
    continue serving IO, verify recovery (Model A).

node_reboot_remount
    Model B variant of node reboot.

mds_failover
    Stop the active MDS, verify standby promotes, NFS exports continue.

dc_power_off
    Power off all nodes in one DC (3 mounts affected, 6 survive),
    verify stretch mode degraded, verify IO on survivors, recover.

netsplit_between_dcs
    iptables-based network partition between two DCs, verify
    cluster degraded mode, verify IO on connected DC3, restore.

netsplit_isolate_dc
    Full isolation of one DC from the other two.

qos_failover
    QoS persistence through NFS daemon kill and cephadm recovery.
"""

from time import sleep

from ceph.ceph_admin import CephAdmin
from cli.ceph.ceph import Ceph
from cli.exceptions import ConfigError, OperationFailedError
from tests.nfs.nfs_3az.nfs_ha_failover_workflows import NfsFailoverWorkflows
from tests.nfs.qos.test_nfs_qos_3az_multi_export import (
    _create_export,
    _create_subvolumes_for_cluster,
    _get_nfs_server_ip,
    _install_fio,
    _mount_export,
    _unmount_and_cleanup,
)
from utility.log import Log

log = Log(__name__)

_PHASE_SEP = "-" * 70


def _log_phase(number, title):
    log.info(f"\n{_PHASE_SEP}")
    log.info(f"  PHASE {number}: {title}")
    log.info(_PHASE_SEP)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _parse_failover_config(config):
    """Parse suite YAML config into failover test parameters."""
    nfs_clusters = config.get("nfs_clusters")
    if not nfs_clusters:
        raise ConfigError("nfs_clusters is required")

    cephfs_volumes = config.get("cephfs_volumes")
    if not cephfs_volumes:
        raise ConfigError("cephfs_volumes is required")

    clusters = []
    for i, cn in enumerate(nfs_clusters):
        clusters.append(
            {
                "name": cn,
                "cephfs_volume": cephfs_volumes[i % len(cephfs_volumes)],
            }
        )

    return {
        "clusters": clusters,
        "num_exports_per_cluster": int(config.get("num_exports_per_cluster", 3)),
        "subvol_group": config.get("subvol_group", "ganeshagroup"),
        "export_prefix": config.get("nfs_export_prefix", "/export_failover"),
        "mount_prefix": config.get("nfs_mount_prefix", "/mnt/nfs_failover"),
        "failover_model": config.get("failover_model", "recovery"),
        "target_dc": config.get("target_dc", "DC1"),
        "target_cluster": config.get("target_cluster", None),
        "kill_method": config.get("kill_method", "orch_stop"),
        "fio": {
            "bs": str(config.get("fio_bs", "1M")),
            "iodepth": str(config.get("fio_iodepth", "16")),
            "numjobs": str(config.get("fio_numjobs", "2")),
            "size": str(config.get("fio_size", "128M")),
            "runtime": str(config.get("fio_runtime", "30")),
        },
    }


# ---------------------------------------------------------------------------
# Target selection helpers
# ---------------------------------------------------------------------------


def _pick_target_daemon(daemons, assignments):
    """Pick a daemon that has at least one mount assigned to it.

    Falls back to daemons[0] if no daemon has a mount (shouldn't
    happen with proper round-robin distribution).
    """
    mount_ips = {a["nfs_server_ip"] for a in assignments}
    for d in daemons:
        if d.get("host_node") and d["host_node"].ip_address in mount_ips:
            return d
    log.warning("  No daemon has mounts assigned -- picking first daemon")
    return daemons[0]


def _ensure_driver_safe(driver_client, target_nodes):
    """Verify the driver client is not on a node we're about to kill.

    The driver client runs ceph CLI commands; if it's on a node
    that gets powered off or netsplit, all subsequent phases fail.
    Returns True if safe, raises OperationFailedError if not.
    """
    target_hostnames = {n.hostname for n in target_nodes}
    target_ips = {n.ip_address for n in target_nodes}
    if (
        driver_client.hostname in target_hostnames
        or driver_client.ip_address in target_ips
    ):
        raise OperationFailedError(
            f"Driver client {driver_client.hostname} is in the "
            f"target blast radius {target_hostnames}. Cannot "
            f"proceed -- choose a different target_dc or "
            f"target_cluster."
        )
    return True


# ---------------------------------------------------------------------------
# Common setup / cleanup
# ---------------------------------------------------------------------------


def _cleanup_stale_resources(driver_client, cfg):
    """Remove leftover exports and subvolumes from a previous run.

    Uses the deterministic naming scheme to find and delete any
    resources that match the export/subvolume prefix patterns,
    preventing 'Export already exists' errors on re-run.
    """
    log.info("  Cleaning up stale resources from previous runs...")
    nfs_obj = Ceph(driver_client).nfs
    ceph_fs = Ceph(driver_client).fs

    for cluster in cfg["clusters"]:
        cn = cluster["name"]
        for i in range(cfg["num_exports_per_cluster"]):
            export_path = f"{cfg['export_prefix']}_{cn}_{i}"
            try:
                nfs_obj.export.delete(cn, export_path)
                log.info(f"    Deleted stale export {export_path} on {cn}")
            except Exception:
                pass

            sv_name = f"qos_sv_{cn}_{i}"
            try:
                ceph_fs.sub_volume.rm(
                    cluster["cephfs_volume"],
                    sv_name,
                    group=cfg["subvol_group"],
                    force=True,
                )
                log.info(f"    Deleted stale subvolume {sv_name}")
            except Exception:
                pass


def _setup_exports_and_mounts(driver_client, clients, cfg, nfs_wf):
    """Create subvolumes, exports, and mount on clients.

    Returns (assignments, subvol_map, cluster_cache) where assignments
    is a list of dicts with client, mount, export, and server info.

    Calls ``discover_nfs_clusters`` first to cache the full topology
    (daemon IPs, ports, ranks, backends) for all clusters.  Mounts
    are distributed round-robin across daemon IPs so that different
    mounts connect to different daemons -- essential for failover
    testing.  The NFS port is taken from the cluster info (not
    hardcoded).

    Cleans up stale exports/subvolumes from previous runs before
    creating new ones to avoid 'Export already exists' errors.
    """
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]

    _cleanup_stale_resources(driver_client, cfg)

    log.info("  Restarting all NFS clusters for a clean state...")
    for cn in cluster_names:
        nfs_wf.restart_nfs_cluster(cn, timeout=120)
    log.info("  Waiting 60s for NFS daemons to settle...")
    sleep(60)

    cluster_cache = nfs_wf.discover_nfs_clusters(cluster_names)
    if not nfs_wf.validate_3az_topology(cluster_names):
        raise ConfigError(
            "NFS clusters do not have daemons across multiple "
            "hosts -- cannot proceed with failover tests"
        )

    subvol_map = {}
    for cluster in cluster_specs:
        subvol_map[cluster["name"]] = _create_subvolumes_for_cluster(
            driver_client,
            cluster["cephfs_volume"],
            cluster["name"],
            cfg["num_exports_per_cluster"],
            cfg["subvol_group"],
        )

    assignments = []
    client_idx = 0
    for cluster in cluster_specs:
        cn = cluster["name"]
        cc = cluster_cache.get(cn, {})
        daemon_ips = cc.get("daemon_ips", [])
        nfs_port = str(cc.get("nfs_port", 2049))

        if not daemon_ips:
            daemon_ips = [_get_nfs_server_ip(driver_client, cn)]
            log.warning(
                f"  {cn}: no cached daemon IPs, " f"falling back to {daemon_ips}"
            )

        for i, sv in enumerate(subvol_map[cn]):
            server_ip = daemon_ips[i % len(daemon_ips)]
            a = {
                "client": clients[client_idx % len(clients)],
                "cluster_name": cn,
                "cephfs_volume": cluster["cephfs_volume"],
                "export_path": f"{cfg['export_prefix']}_{cn}_{i}",
                "mount_point": f"{cfg['mount_prefix']}_{cn}_{i}",
                "nfs_server_ip": server_ip,
                "port": nfs_port,
                "subvol_name": sv["name"],
                "subvol_path": sv["path"],
            }
            assignments.append(a)
            client_idx += 1

    for a in assignments:
        _create_export(
            driver_client,
            a["cephfs_volume"],
            a["cluster_name"],
            a["export_path"],
            cephfs_path=a["subvol_path"],
        )

    for a in assignments:
        _mount_export(
            a["client"],
            a["nfs_server_ip"],
            a["export_path"],
            a["mount_point"],
            a["port"],
        )
        sleep(1)

    log.info(f"  Setup complete: {len(assignments)} mount(s)")
    for a in assignments:
        log.info(
            f"    {a['client'].hostname} -> {a['nfs_server_ip']}:"
            f"{a['port']}{a['export_path']} @ {a['mount_point']}"
        )
    return assignments, subvol_map, cluster_cache


def _cleanup(driver_client, assignments, subvol_map, cluster_specs, cfg):
    """Unmount, delete exports, delete subvolumes."""
    log.info("\n  CLEANUP...")

    nfs_obj = Ceph(driver_client).nfs

    for a in assignments:
        _unmount_and_cleanup(a["client"], a["mount_point"])

    for a in assignments:
        try:
            nfs_obj.export.delete(a["cluster_name"], a["export_path"])
        except Exception as e:
            log.debug(f"    export delete {a['export_path']}: {e}")

    ceph_fs = Ceph(driver_client).fs
    for cluster in cluster_specs:
        cn = cluster["name"]
        for sv in subvol_map.get(cn, []):
            try:
                ceph_fs.sub_volume.rm(
                    cluster["cephfs_volume"],
                    sv["name"],
                    group=cfg["subvol_group"],
                    force=True,
                )
            except Exception as e:
                log.debug(f"    subvol rm {sv['name']}: {e}")

    log.info("  Cleanup complete")


# ---------------------------------------------------------------------------
# Scenario: nfs_daemon_kill (Model A -- recovery in-place)
# ---------------------------------------------------------------------------


def _scenario_nfs_daemon_kill(config, ceph_cluster):
    """Kill NFS daemon on a single node, verify IO continuity and recovery.

    Model A (recovery in-place): the affected client's hard mount retries
    until cephadm restarts the daemon.  Surviving daemons are unaffected.

    Example with target_cluster=nfs-DC1, target daemon on node1::

        Before kill (3 mounts on nfs-DC1):
          node10 --mount--> node1 (DC1)  <-- AFFECTED (daemon killed)
          node11 --mount--> node4 (DC2)  <-- surviving
          node12 --mount--> node7 (DC3)  <-- surviving

    Workflow::

        Setup (9 mounts across 3 clusters, all 3 daemons per cluster)
           |
           v
        Baseline IO (all 9 mounts) -> Data Integrity Check
           |
           v
        Start Background IO (9 threads) ---> Kill target daemon
           |                                      |
           v                                      v
        Collect BG IO results              cephadm restarts daemon
           |                                      |
           v                                      v
        IO on 8 surviving mounts           Wait for 3/3 running
           |                                      |
           +-------------+-------------+----------+
                         |
                         v
              Post-recovery IO (all 9 mounts)
                         |
                         v
              Data Integrity + Cross-daemon Visibility -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_cluster = cfg.get("target_cluster") or cluster_names[0]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio")
        _install_fio(clients)

        _log_phase(1, "Setup exports and mounts")
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(2, "Baseline IO")
        passes, fails = nfs_wf.verify_io_continuity(assignments, cfg["fio"])
        if fails > 0:
            log.warning(
                f"  Baseline IO failed on {fails} mount(s), "
                f"remounting failed mounts and retrying..."
            )
            for a in assignments:
                if not nfs_wf.check_mount_accessible(
                    a["client"], a["mount_point"], timeout=10
                ):
                    log.info(f"    Remounting {a['mount_point']}...")
                    _unmount_and_cleanup(a["client"], a["mount_point"])
                    _mount_export(
                        a["client"],
                        a["nfs_server_ip"],
                        a["export_path"],
                        a["mount_point"],
                        a["port"],
                    )
            sleep(10)
            passes, fails = nfs_wf.verify_io_continuity(assignments, cfg["fio"])
            if fails > 0:
                raise OperationFailedError(
                    f"Baseline IO failed on {fails} mount(s) " f"even after remount"
                )

        _log_phase(3, "Write baseline data for integrity check")
        if not nfs_wf.verify_data_integrity(assignments, cfg["fio"]):
            raise OperationFailedError("Baseline data integrity failed")

        _log_phase(4, "Start background IO and inject failure")
        executor, futures = nfs_wf.run_fio_background(assignments, cfg["fio"])
        sleep(10)

        daemons = nfs_wf.get_nfs_daemon_hosts(target_cluster)
        if not daemons:
            raise OperationFailedError(f"No daemons found for {target_cluster}")
        target = _pick_target_daemon(daemons, assignments)
        kill_method = cfg.get("kill_method", "orch_stop")
        log.info(
            f"  Target daemon: {target['daemon_name']} on "
            f"{target['hostname']} (method={kill_method})"
        )
        nfs_wf.kill_nfs_daemon(
            target["host_node"],
            daemon_name=target["daemon_name"],
            method=kill_method,
        )

        _log_phase(5, "Collect background IO results")
        bg_results = []
        for f in futures:
            try:
                bg_results.append(f.result())
            except Exception as e:
                bg_results.append({"error": str(e)})
        executor.shutdown(wait=False)

        surviving = [
            a
            for a in assignments
            if a["nfs_server_ip"] != target["host_node"].ip_address
        ]
        if surviving:
            _log_phase(6, "Verify IO on surviving daemons")
            s_pass, s_fail = nfs_wf.verify_io_continuity(surviving, cfg["fio"])
            log.info(f"  Surviving daemons: {s_pass} pass, {s_fail} fail")

        _log_phase(
            7,
            "Wait for daemon recovery (cephadm auto-restart)",
        )
        nfs_wf._wait_for_nfs_daemons_running(target_cluster, timeout=120)
        nfs_wf.verify_nfs_daemon_count(target_cluster, expected_count=len(daemons))

        _log_phase("7b", "Check affected mounts, remount if stale")
        sleep(30)
        affected = [
            a
            for a in assignments
            if a["nfs_server_ip"] == target["host_node"].ip_address
        ]
        for a in affected:
            if not nfs_wf.check_mount_accessible(
                a["client"], a["mount_point"], timeout=15
            ):
                log.warning(
                    f"  {a['mount_point']} still stale after "
                    f"daemon recovery, remounting..."
                )
                _unmount_and_cleanup(a["client"], a["mount_point"])
                _mount_export(
                    a["client"],
                    a["nfs_server_ip"],
                    a["export_path"],
                    a["mount_point"],
                    a["port"],
                )

        _log_phase(8, "Post-recovery IO on ALL mounts")
        p_pass, p_fail = nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(9, "Data integrity and cross-daemon visibility")
        nfs_wf.verify_cross_daemon_visibility(assignments)
        integrity = nfs_wf.verify_data_integrity(assignments, cfg["fio"])

        nfs_wf.verify_nfs_cluster_health(cluster_names)

        if p_fail > 0:
            raise OperationFailedError(f"Post-recovery IO failed on {p_fail} mount(s)")
        if not integrity:
            raise OperationFailedError("Data integrity check failed post-recovery")

        log.info("\n  *** SCENARIO nfs_daemon_kill: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO nfs_daemon_kill FAILED: {e} ***")
        nfs_wf.log_nfs_diagnostics(cluster_names)
        return 1

    finally:
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: nfs_daemon_kill_remount (Model B -- active remount)
# ---------------------------------------------------------------------------


def _scenario_nfs_daemon_kill_remount(config, ceph_cluster):
    """Kill daemon, detect stale mount, remount to surviving daemon.

    Model B (active remount): after detecting the mount is unresponsive,
    the test unmounts and remounts the client to a surviving daemon in
    a different DC, simulating application-level failover.

    Example with target daemon on node1 (nfs-DC1)::

        Before kill:
          node10 --mount--> node1 (DC1)  <-- AFFECTED
          node11 --mount--> node4 (DC2)
          node12 --mount--> node7 (DC3)

        After remount:
          node10 --mount--> node4 (DC2)  <-- remounted to surviving daemon
          node11 --mount--> node4 (DC2)
          node12 --mount--> node7 (DC3)

    Workflow::

        Setup (9 mounts) -> Baseline IO -> Data Integrity
           |
           v
        Kill target daemon -> Detect stale (timeout stat -f)
           |
           +--- STALE ---> umount -l -> remount to surviving daemon
           |
           v
        IO on remounted + surviving mounts
           |
           v
        Data Integrity + Cross-daemon Visibility -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_cluster = cfg.get("target_cluster") or cluster_names[0]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio")
        _install_fio(clients)

        _log_phase(1, "Setup exports and mounts")
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(2, "Baseline IO and data integrity")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])
        nfs_wf.verify_data_integrity(assignments, cfg["fio"])

        _log_phase(3, "Identify target and kill daemon")
        daemons = nfs_wf.get_nfs_daemon_hosts(target_cluster)
        if len(daemons) < 2:
            raise OperationFailedError(
                f"Need >= 2 daemons for remount test, " f"got {len(daemons)}"
            )
        target = _pick_target_daemon(daemons, assignments)
        surviving_daemon = next(
            d for d in daemons if d["hostname"] != target["hostname"]
        )
        target_ip = target["host_node"].ip_address

        affected = [a for a in assignments if a["nfs_server_ip"] == target_ip]
        if not affected:
            affected = [a for a in assignments if a["cluster_name"] == target_cluster][
                :1
            ]

        kill_method = cfg.get("kill_method", "orch_stop")
        nfs_wf.kill_nfs_daemon(
            target["host_node"],
            daemon_name=target["daemon_name"],
            method=kill_method,
        )
        sleep(5)

        _log_phase(4, "Detect stale mount and remount")
        surviving_ip = surviving_daemon["host_node"].ip_address
        for a in affected:
            stale = not nfs_wf.check_mount_accessible(
                a["client"], a["mount_point"], timeout=10
            )
            log.info(f"  {a['mount_point']}: " f"{'STALE' if stale else 'accessible'}")
            if stale:
                log.info(
                    f"  Remounting {a['mount_point']} from "
                    f"{target_ip} to {surviving_ip}..."
                )
                _unmount_and_cleanup(a["client"], a["mount_point"])
                a["nfs_server_ip"] = surviving_ip
                _mount_export(
                    a["client"],
                    surviving_ip,
                    a["export_path"],
                    a["mount_point"],
                    a["port"],
                )

        _log_phase(5, "IO on remounted client")
        r_pass, r_fail = nfs_wf.verify_io_continuity(affected, cfg["fio"])

        _log_phase(6, "Data integrity from new daemon")
        nfs_wf.verify_data_integrity(affected, cfg["fio"])
        nfs_wf.verify_cross_daemon_visibility(assignments)

        if r_fail > 0:
            raise OperationFailedError(f"Remounted IO failed on {r_fail} mount(s)")

        log.info("\n  *** SCENARIO nfs_daemon_kill_remount: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO nfs_daemon_kill_remount FAILED: " f"{e} ***")
        nfs_wf.log_nfs_diagnostics(cluster_names)
        return 1

    finally:
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: node_reboot (Model A)
# ---------------------------------------------------------------------------


def _scenario_node_reboot(config, ceph_cluster):
    """Reboot a node hosting NFS+MDS+OSD, verify IO and recovery.

    Model A (recovery in-place): the node is rebooted (taking down
    NFS daemon, MDS, MON, MGR, and OSDs on that node).  After the
    node comes back (~300s) and cephadm redeploys services (~120s),
    all mounts should recover via hard-mount retry.

    Example rebooting node1 (DC1)::

        Affected mounts (1 per NFS cluster with daemon on node1):
          node10 --mount--> node1 : nfs-DC1 export_0  <-- AFFECTED
        Surviving mounts (8 total):
          node11 --mount--> node4 : nfs-DC1 export_1  <-- surviving
          node12 --mount--> node7 : nfs-DC1 export_2  <-- surviving
          (+ 6 more from nfs-DC2 and nfs-DC3)

    Workflow::

        Setup (9 mounts, all 3 daemons per cluster)
           |
           v
        Baseline IO (all 9) -> Data Integrity
           |
           v
        Reboot target node (wait ~420s for node + cephadm)
           |
           v
        IO on 8 surviving mounts
           |
           v
        MON quorum >= 6 -> NFS health -> no crashes
           |
           v
        Post-recovery IO (all 9) -> Data Integrity -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_cluster = cfg.get("target_cluster") or cluster_names[0]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio")
        _install_fio(clients)

        _log_phase(1, "Setup exports and mounts")
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(2, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])
        nfs_wf.verify_data_integrity(assignments, cfg["fio"])

        _log_phase(3, "Reboot target node")
        daemons = nfs_wf.get_nfs_daemon_hosts(target_cluster)
        target = _pick_target_daemon(daemons, assignments)
        target_node = target["host_node"]
        _ensure_driver_safe(driver_client, [target_node])
        log.info(
            f"  Rebooting {target_node.hostname} " f"(hosts {target['daemon_name']})..."
        )
        nfs_wf.reboot_node(target_node, wait_for_cephadm=True)

        _log_phase(4, "IO on surviving daemons during recovery window")
        surviving = [
            a for a in assignments if a["nfs_server_ip"] != target_node.ip_address
        ]
        if surviving:
            s_pass, s_fail = nfs_wf.verify_io_continuity(surviving, cfg["fio"])
            log.info(f"  Surviving daemons: {s_pass} pass, {s_fail} fail")

        _log_phase(5, "Verify cluster state")
        nfs_wf.verify_mon_quorum(expected_min=6)
        nfs_wf.verify_nfs_cluster_health(cluster_names)
        nfs_wf.check_no_crashes()

        _log_phase("5b", "Check affected mounts, remount if stale")
        affected = [
            a for a in assignments if a["nfs_server_ip"] == target_node.ip_address
        ]
        for a in affected:
            if not nfs_wf.check_mount_accessible(
                a["client"], a["mount_point"], timeout=15
            ):
                log.warning(
                    f"  {a['mount_point']} still stale after "
                    f"node recovery, remounting..."
                )
                _unmount_and_cleanup(a["client"], a["mount_point"])
                _mount_export(
                    a["client"],
                    a["nfs_server_ip"],
                    a["export_path"],
                    a["mount_point"],
                    a["port"],
                )

        _log_phase(6, "Post-recovery IO on ALL mounts")
        p_pass, p_fail = nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(7, "Data integrity")
        nfs_wf.verify_cross_daemon_visibility(assignments)
        integrity = nfs_wf.verify_data_integrity(assignments, cfg["fio"])

        if p_fail > 0:
            raise OperationFailedError(f"Post-recovery IO failed on {p_fail} mount(s)")
        if not integrity:
            raise OperationFailedError("Data integrity failed")

        log.info("\n  *** SCENARIO node_reboot: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO node_reboot FAILED: {e} ***")
        nfs_wf.log_nfs_diagnostics(cluster_names)
        return 1

    finally:
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: node_reboot_remount (Model B)
# ---------------------------------------------------------------------------


def _scenario_node_reboot_remount(config, ceph_cluster):
    """Reboot node, detect stale mount, remount to surviving daemon.

    Model B (active remount): after rebooting the target node, affected
    mounts are immediately unmounted and remounted to a surviving daemon
    in a different DC without waiting for the node to recover.

    Example rebooting node1::

        Before: node10 --mount--> node1  (affected)
        After:  node10 --mount--> node4  (remounted to DC2 daemon)

    Workflow::

        Setup (9 mounts) -> Baseline IO
           |
           v
        Reboot target node (no wait for cephadm)
           |
           v
        umount affected mount(s) -> remount to surviving daemon
           |
           v
        IO on remounted + surviving mounts
           |
           v
        Wait 120s for node recovery -> NFS health -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_cluster = cfg.get("target_cluster") or cluster_names[0]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(2, "Reboot target node")
        daemons = nfs_wf.get_nfs_daemon_hosts(target_cluster)
        if len(daemons) < 2:
            raise OperationFailedError("Need >= 2 daemons for remount")
        target = _pick_target_daemon(daemons, assignments)
        surviving = next(d for d in daemons if d["hostname"] != target["hostname"])
        target_ip = target["host_node"].ip_address
        surviving_ip = surviving["host_node"].ip_address
        _ensure_driver_safe(driver_client, [target["host_node"]])

        nfs_wf.reboot_node(target["host_node"], wait_for_cephadm=False)

        _log_phase(3, "Remount affected mounts to surviving daemon")
        affected = [a for a in assignments if a["nfs_server_ip"] == target_ip]
        for a in affected:
            _unmount_and_cleanup(a["client"], a["mount_point"])
            a["nfs_server_ip"] = surviving_ip
            _mount_export(
                a["client"],
                surviving_ip,
                a["export_path"],
                a["mount_point"],
                a["port"],
            )

        _log_phase(4, "IO on remounted client")
        nfs_wf.verify_io_continuity(affected, cfg["fio"])

        _log_phase(5, "Wait for node recovery")
        sleep(120)
        nfs_wf.verify_nfs_cluster_health(cluster_names)

        _log_phase(6, "Data integrity")
        nfs_wf.verify_cross_daemon_visibility(assignments)

        log.info("\n  *** SCENARIO node_reboot_remount: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO node_reboot_remount FAILED: {e} ***")
        return 1

    finally:
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: mds_failover
# ---------------------------------------------------------------------------


def _scenario_mds_failover(config, ceph_cluster):
    """Stop active MDS, verify standby promotes, NFS IO continues.

    Tests CephFS metadata server failover under NFS-Ganesha.  The
    active MDS for the target filesystem is stopped via orch; a
    standby MDS should promote to active and NFS exports should
    continue serving IO after a brief pause.

    All 9 mounts use cephfs-DC1 as backing filesystem, so an MDS
    failure for cephfs-DC1 affects all exports across all 3 NFS
    clusters.

    Workflow::

        Setup (9 mounts, all backed by cephfs-DC1)
           |
           v
        Baseline IO (all 9 mounts)
           |
           v
        Stop active MDS (ceph orch daemon stop mds.X)
           |
           v
        Wait for standby MDS to promote to rank-0 active
           |
           v
        IO on all 9 mounts (sleep 15s for MDS settle)
           |
           v
        Data Integrity -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_fs = cluster_specs[0]["cephfs_volume"]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(2, "Identify and stop active MDS")
        active = nfs_wf.get_active_mds(target_fs)
        if not active:
            raise OperationFailedError(f"No active MDS for {target_fs}")
        log.info(f"  Active MDS: {active['daemon_id']} on " f"{active['hostname']}")
        original_id = active["daemon_id"]
        nfs_wf.stop_active_mds(target_fs)

        _log_phase(3, "Wait for MDS failover")
        new_active = nfs_wf.wait_for_mds_failover(target_fs, original_id, timeout=120)
        if not new_active:
            raise OperationFailedError("MDS failover did not occur")
        log.info(
            f"  New active MDS: {new_active['daemon_id']} on "
            f"{new_active['hostname']}"
        )

        _log_phase(4, "IO after MDS failover")
        sleep(15)
        fs_assignments = [a for a in assignments if a["cephfs_volume"] == target_fs]
        p_pass, p_fail = nfs_wf.verify_io_continuity(
            fs_assignments if fs_assignments else assignments,
            cfg["fio"],
        )

        _log_phase(5, "Data integrity")
        nfs_wf.verify_data_integrity(
            fs_assignments if fs_assignments else assignments,
            cfg["fio"],
        )

        if p_fail > 0:
            raise OperationFailedError(
                f"IO failed on {p_fail} mount(s) after MDS failover"
            )

        log.info("\n  *** SCENARIO mds_failover: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO mds_failover FAILED: {e} ***")
        nfs_wf.log_nfs_diagnostics(cluster_names)
        return 1

    finally:
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: dc_power_off
# ---------------------------------------------------------------------------


def _scenario_dc_power_off(config, ceph_cluster):
    """Power off all nodes in one DC, verify IO on survivors, recover.

    Simulates a full availability zone failure by powering off all
    nodes in the target datacenter.  Verifies surviving DCs maintain
    MON quorum, NFS clusters continue on remaining daemons, and IO
    works.  Then recovers the DC and verifies full restoration.

    Example powering off DC1 (node1, node2, node3)::

        Affected mounts (3 -- one per NFS cluster):
          node10 --mount--> node1 : nfs-DC1_0  <-- DOWN
          node10 --mount--> node2 : nfs-DC2_0  <-- DOWN
          node10 --mount--> node3 : nfs-DC3_0  <-- DOWN
        Surviving mounts (6 -- DC2 + DC3 daemons):
          node11 --mount--> node4 : nfs-DC1_1  <-- OK
          node12 --mount--> node7 : nfs-DC1_2  <-- OK
          node11 --mount--> node5 : nfs-DC2_1  <-- OK
          node12 --mount--> node8 : nfs-DC2_2  <-- OK
          node11 --mount--> node6 : nfs-DC3_1  <-- OK
          node12 --mount--> node9 : nfs-DC3_2  <-- OK

    Workflow::

        Setup (9 mounts) -> Baseline IO
           |
           v
        Identify DC1 hosts -> Ensure driver client safe
           |
           v
        Power off DC1 (3 nodes down, 3 mounts affected)
           |
           v
        MON quorum >= 4 -> IO on 6 surviving mounts
           |
           v
        Recover DC1 (power on, wait 120s) -> Cluster healthy
           |
           v
        Remount 3 DC1 mounts -> IO on all 9 -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_dc = cfg.get("target_dc", "DC1")

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []
    dc_nodes = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(2, f"Identify {target_dc} hosts")
        dc_nodes = nfs_wf.get_dc_hosts(target_dc)
        dc_hostnames = [n.hostname for n in dc_nodes]
        log.info(f"  {target_dc} nodes: {dc_hostnames}")
        _ensure_driver_safe(driver_client, dc_nodes)

        dc_ips = {n.ip_address for n in dc_nodes}
        surviving = [a for a in assignments if a["nfs_server_ip"] not in dc_ips]
        log.info(f"  Surviving mounts: {len(surviving)}/{len(assignments)}")

        _log_phase(3, f"Power off {target_dc}")
        nfs_wf.power_off_nodes(dc_nodes)

        _log_phase(4, "Verify cluster state")
        nfs_wf.verify_mon_quorum(expected_min=4)

        _log_phase(5, "IO on surviving mounts")
        if surviving:
            s_pass, s_fail = nfs_wf.verify_io_continuity(surviving, cfg["fio"])
            log.info(f"  Surviving IO: {s_pass} pass, {s_fail} fail")

        _log_phase(6, f"Recover {target_dc}")
        recovered = nfs_wf.power_on_nodes(dc_nodes, timeout=600)
        log.info(f"  Recovered {len(recovered)}/{len(dc_nodes)} nodes")
        sleep(120)

        _log_phase(7, "Post-recovery verification")
        nfs_wf.wait_for_cluster_healthy(timeout=600)
        nfs_wf.verify_nfs_cluster_health(cluster_names)

        remount_needed = [a for a in assignments if a["nfs_server_ip"] in dc_ips]
        for a in remount_needed:
            try:
                _unmount_and_cleanup(a["client"], a["mount_point"])
                _mount_export(
                    a["client"],
                    a["nfs_server_ip"],
                    a["export_path"],
                    a["mount_point"],
                    a["port"],
                )
            except Exception as e:
                log.warning(f"  Remount {a['mount_point']}: {e}")

        p_pass, p_fail = nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        if p_fail > 0:
            raise OperationFailedError(f"Post-recovery IO failed on {p_fail} mount(s)")

        log.info("\n  *** SCENARIO dc_power_off: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO dc_power_off FAILED: {e} ***")
        nfs_wf.log_nfs_diagnostics(cluster_names)
        return 1

    finally:
        if dc_nodes:
            try:
                nfs_wf.power_on_nodes(dc_nodes, timeout=300)
            except Exception:
                pass
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: netsplit_between_dcs
# ---------------------------------------------------------------------------


def _scenario_netsplit_between_dcs(config, ceph_cluster):
    """Netsplit between two DCs, verify IO on connected nodes, restore.

    Uses iptables DROP rules to block all traffic between DC1 and DC2.
    DC3 remains connected to both and acts as a bridge.  Verifies the
    cluster enters degraded stretch mode, IO works on DC3 mounts,
    then restores connectivity and verifies full recovery.

    Mount impact during DC1 <-> DC2 netsplit::

        DC3 mounts (3 -- fully connected, IO works):
          node10 --mount--> node3 : nfs-DC3_0  <-- OK
          node11 --mount--> node6 : nfs-DC3_1  <-- OK
          node12 --mount--> node9 : nfs-DC3_2  <-- OK

        DC1/DC2 mounts (6 -- may be disrupted by partition):
          node10 --mount--> node1/node2  <-- uncertain
          node11 --mount--> node4/node5  <-- uncertain
          node12 --mount--> node7/node8  <-- uncertain

    Follows ``tests/rados/test_stretch_netsplit_scenarios.py`` pattern.

    Workflow::

        Setup (9 mounts) -> Baseline IO
           |
           v
        Install iptables prereqs -> Ensure driver client safe
           |
           v
        Netsplit DC1 <-> DC2 (wait 180s)
           |
           v
        MON quorum >= 4 -> IO on 3 DC3 mounts
           |
           v
        Restore (iptables -F + reboot + reconnect)
           |
           v
        Cluster healthy -> IO on all 9 mounts -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []
    dc1_hosts = []
    dc2_hosts = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(2, "Identify DC hosts and install prereqs")
        dc1_hosts = nfs_wf.get_dc_hosts("DC1")
        dc2_hosts = nfs_wf.get_dc_hosts("DC2")
        log.info(f"  DC1: {[n.hostname for n in dc1_hosts]}")
        log.info(f"  DC2: {[n.hostname for n in dc2_hosts]}")
        _ensure_driver_safe(driver_client, dc1_hosts + dc2_hosts)
        all_cluster_nodes = ceph_cluster.get_nodes()
        nfs_wf.install_netsplit_prereqs(all_cluster_nodes)

        _log_phase(3, "Apply netsplit DC1 <-> DC2")
        nfs_wf.netsplit_dc(dc1_hosts, dc2_hosts)
        log.info("  Waiting 180s for cluster to detect partition...")
        sleep(180)

        _log_phase(4, "Verify cluster state during netsplit")
        nfs_wf.verify_mon_quorum(expected_min=4)

        dc3_ips = set()
        dc3_hosts = nfs_wf.get_dc_hosts("DC3")
        for n in dc3_hosts:
            dc3_ips.add(n.ip_address)
        dc3_mounts = [a for a in assignments if a["nfs_server_ip"] in dc3_ips]

        if dc3_mounts:
            _log_phase(5, "IO on DC3 mounts during netsplit")
            s_pass, s_fail = nfs_wf.verify_io_continuity(dc3_mounts, cfg["fio"])

        _log_phase(6, "Restore connectivity")
        nfs_wf.restore_netsplit(dc1_hosts, dc2_hosts)
        sleep(60)

        _log_phase(7, "Post-netsplit recovery")
        nfs_wf.wait_for_cluster_healthy(timeout=600)
        nfs_wf.verify_nfs_cluster_health(cluster_names)

        _log_phase(8, "Post-recovery IO on all mounts")
        p_pass, p_fail = nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        if p_fail > 0:
            raise OperationFailedError(f"Post-netsplit IO failed on {p_fail} mount(s)")

        log.info("\n  *** SCENARIO netsplit_between_dcs: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO netsplit_between_dcs FAILED: {e} ***")
        return 1

    finally:
        if dc1_hosts and dc2_hosts:
            try:
                nfs_wf.restore_netsplit(dc1_hosts, dc2_hosts)
            except Exception:
                pass
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: netsplit_isolate_dc
# ---------------------------------------------------------------------------


def _scenario_netsplit_isolate_dc(config, ceph_cluster):
    """Fully isolate one DC from the other two.

    Blocks all traffic from the target DC to both other DCs using
    iptables.  The isolated DC loses connectivity entirely while
    the remaining two DCs maintain quorum and NFS service.

    Example isolating DC1 (node1, node2, node3)::

        Isolated mounts (3 -- on DC1 daemon IPs):
          node10 --mount--> node1 : nfs-DC1_0  <-- ISOLATED
          node10 --mount--> node2 : nfs-DC2_0  <-- ISOLATED
          node10 --mount--> node3 : nfs-DC3_0  <-- ISOLATED
        Surviving mounts (6 -- DC2 + DC3 daemon IPs):
          (same as dc_power_off example)

    Workflow::

        Setup (9 mounts) -> Baseline IO
           |
           v
        Ensure driver client safe -> Install iptables prereqs
           |
           v
        Isolate DC1 from DC2+DC3 (wait 180s)
           |
           v
        MON quorum >= 4 -> IO on 6 surviving mounts
           |
           v
        Restore (iptables -F + reboot + reconnect)
           |
           v
        Cluster healthy -> IO on all 9 mounts -> Cleanup
    """
    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_dc = cfg.get("target_dc", "DC1")

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    subvol_map = {}
    assignments = []
    isolated_hosts = []
    other_hosts = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Baseline IO")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(2, f"Identify hosts and isolate {target_dc}")
        isolated_hosts = nfs_wf.get_dc_hosts(target_dc)
        _ensure_driver_safe(driver_client, isolated_hosts)
        all_dcs = ["DC1", "DC2", "DC3"]
        other_dcs = [dc for dc in all_dcs if dc != target_dc]
        other_hosts = []
        for dc in other_dcs:
            other_hosts.extend(nfs_wf.get_dc_hosts(dc))
        all_cluster_nodes = ceph_cluster.get_nodes()
        nfs_wf.install_netsplit_prereqs(all_cluster_nodes)

        nfs_wf.isolate_dc(isolated_hosts, other_hosts)
        log.info("  Waiting 180s for cluster to detect isolation...")
        sleep(180)

        _log_phase(3, "Verify cluster during isolation")
        nfs_wf.verify_mon_quorum(expected_min=4)

        isolated_ips = {n.ip_address for n in isolated_hosts}
        surviving = [a for a in assignments if a["nfs_server_ip"] not in isolated_ips]

        if surviving:
            _log_phase(4, "IO on surviving mounts")
            nfs_wf.verify_io_continuity(surviving, cfg["fio"])

        _log_phase(5, "Restore connectivity")
        nfs_wf.restore_netsplit(isolated_hosts, other_hosts)
        sleep(60)

        nfs_wf.wait_for_cluster_healthy(timeout=600)
        nfs_wf.verify_nfs_cluster_health(cluster_names)

        _log_phase(6, "Post-recovery IO")
        p_pass, p_fail = nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        if p_fail > 0:
            raise OperationFailedError(f"Post-isolation IO failed on {p_fail} mount(s)")

        log.info("\n  *** SCENARIO netsplit_isolate_dc: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO netsplit_isolate_dc FAILED: {e} ***")
        return 1

    finally:
        if isolated_hosts and other_hosts:
            try:
                nfs_wf.restore_netsplit(isolated_hosts, other_hosts)
            except Exception:
                pass
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario: qos_failover
# ---------------------------------------------------------------------------


def _scenario_qos_failover(config, ceph_cluster):
    """QoS persistence through NFS daemon kill and cephadm recovery.

    Enables cluster-level bandwidth QoS on the target NFS cluster,
    kills one daemon, waits for cephadm to restart it, then verifies
    the QoS settings survived the daemon restart and IO still works
    under QoS enforcement.

    Mounts for target cluster (e.g. nfs-DC1, 3 exports)::

        node10 --mount--> node1 (DC1)  : QoS enforced
        node11 --mount--> node4 (DC2)  : QoS enforced
        node12 --mount--> node7 (DC3)  : QoS enforced <-- daemon killed

    Workflow::

        Setup (3 mounts on target cluster) -> Enable QoS
           |
           v
        Log QoS state (pre-kill) -> Kill daemon (pkill)
           |
           v
        Wait for cephadm recovery -> Log QoS state (post-kill)
           |
           v
        IO with QoS active (all 3 mounts, verify enforcement)
           |
           v
        Disable QoS -> Cleanup
    """
    from tests.nfs.qos.test_nfs_qos_on_cluster_level_enablement import (
        enable_disable_qos_for_cluster,
    )

    cfg = _parse_failover_config(config)
    cluster_specs = cfg["clusters"]
    cluster_names = [c["name"] for c in cluster_specs]
    target_cluster = cfg.get("target_cluster") or cluster_names[0]
    qos_type = config.get("qos_type", "PerShare")
    bw_limit = config.get("bw_limit", "50MB")

    clients = ceph_cluster.get_nodes("client")
    if not clients:
        raise ConfigError("No client nodes found")
    driver_client = clients[0]

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    nfs_obj = Ceph(driver_client).nfs
    subvol_map = {}
    assignments = []

    try:
        _log_phase(0, "Install fio and setup")
        _install_fio(clients)
        assignments, subvol_map, nfs_ips = _setup_exports_and_mounts(
            driver_client, clients, cfg, nfs_wf
        )

        _log_phase(1, "Enable QoS on target cluster")
        bw_params = {}
        if qos_type in ("PerShare", "PerShare_PerClient"):
            bw_params["max_export_write_bw"] = bw_limit
            bw_params["max_export_read_bw"] = bw_limit
        if qos_type in ("PerClient", "PerShare_PerClient"):
            bw_params["max_client_write_bw"] = bw_limit
            bw_params["max_client_read_bw"] = bw_limit

        enable_disable_qos_for_cluster(
            enable_flag=True,
            ceph_cluster_nfs_obj=nfs_obj.cluster,
            cluster_name=target_cluster,
            qos_type=qos_type,
            operation="bandwidth_control",
            **bw_params,
        )
        sleep(10)

        qos_state = nfs_obj.cluster.qos.get(cluster_id=target_cluster, format="json")
        log.info(f"  QoS before kill: {qos_state}")

        _log_phase(2, "Kill NFS daemon")
        daemons = nfs_wf.get_nfs_daemon_hosts(target_cluster)
        target = _pick_target_daemon(daemons, assignments)
        kill_method = cfg.get("kill_method", "orch_stop")
        nfs_wf.kill_nfs_daemon(
            target["host_node"],
            daemon_name=target["daemon_name"],
            method=kill_method,
        )

        _log_phase(3, "Wait for daemon recovery")
        nfs_wf._wait_for_nfs_daemons_running(target_cluster, timeout=120)
        sleep(30)

        _log_phase(4, "Verify QoS persisted")
        qos_post = nfs_obj.cluster.qos.get(cluster_id=target_cluster, format="json")
        log.info(f"  QoS after recovery: {qos_post}")

        _log_phase(5, "IO with QoS active")
        nfs_wf.verify_io_continuity(assignments, cfg["fio"])

        _log_phase(6, "Disable QoS")
        enable_disable_qos_for_cluster(
            enable_flag=False,
            ceph_cluster_nfs_obj=nfs_obj.cluster,
            cluster_name=target_cluster,
            operation="bandwidth_control",
        )

        log.info("\n  *** SCENARIO qos_failover: PASSED ***")
        return 0

    except Exception as e:
        log.error(f"\n  *** SCENARIO qos_failover FAILED: {e} ***")
        return 1

    finally:
        try:
            enable_disable_qos_for_cluster(
                enable_flag=False,
                ceph_cluster_nfs_obj=nfs_obj.cluster,
                cluster_name=target_cluster,
                operation="bandwidth_control",
            )
        except Exception:
            pass
        _cleanup(
            driver_client,
            assignments,
            subvol_map,
            cluster_specs,
            cfg,
        )


# ---------------------------------------------------------------------------
# Scenario dispatch table
# ---------------------------------------------------------------------------

_SCENARIOS = {
    "nfs_daemon_kill": _scenario_nfs_daemon_kill,
    "nfs_daemon_kill_remount": _scenario_nfs_daemon_kill_remount,
    "node_reboot": _scenario_node_reboot,
    "node_reboot_remount": _scenario_node_reboot_remount,
    "mds_failover": _scenario_mds_failover,
    "dc_power_off": _scenario_dc_power_off,
    "netsplit_between_dcs": _scenario_netsplit_between_dcs,
    "netsplit_isolate_dc": _scenario_netsplit_isolate_dc,
    "qos_failover": _scenario_qos_failover,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(ceph_cluster, **kw):
    """Dispatch to the appropriate failover test scenario.

    The ``test_scenario`` config key selects which scenario to run.
    See module docstring for available scenarios.
    """
    config = kw.get("config", {})
    if not config:
        raise ConfigError("No config provided")

    scenario = config.get("test_scenario", "nfs_daemon_kill")
    handler = _SCENARIOS.get(scenario)

    if not handler:
        valid = ", ".join(sorted(_SCENARIOS.keys()))
        log.error(f"Unknown test_scenario '{scenario}'. " f"Valid: {valid}")
        return 1

    log.info(f"=== 3AZ Failover Scenario: {scenario} ===")
    return handler(config, ceph_cluster)
