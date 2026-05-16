"""
NFS-Ganesha failover workflow helpers for 3 AZ stretch-mode clusters.

Provides methods for daemon discovery, failure injection, site-level
failure simulation, IO continuity verification, cluster health and
recovery validation, and NFS service checks.

Follows the cephci pattern established by ``RadosOrchestrator``,
``MonitorWorkflows``, ``MgrWorkflows`` -- initialized with a
``CephAdmin`` node, wraps ``RadosOrchestrator`` for cluster operations.

Usage::

    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    nfs_wf = NfsFailoverWorkflows(node=cephadm)
    daemons = nfs_wf.get_nfs_daemon_hosts("nfs-DC1")
    nfs_wf.kill_nfs_daemon(daemons[0]["host_node"])
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread

from ceph.ceph_admin import CephAdmin
from ceph.rados.core_workflows import RadosOrchestrator
from ceph.rados.utils import install_package
from ceph.waiter import WaitUntil
from cli.ceph.ceph import Ceph
from cli.exceptions import OperationFailedError
from cli.utilities.utils import reboot_node as _reboot_node
from utility.log import Log

log = Log(__name__)


class NfsFailoverWorkflows:
    """NFS-Ganesha failover workflow helpers for 3 AZ stretch-mode clusters."""

    def __init__(self, node: CephAdmin):
        self.node = node
        self.rados_obj = RadosOrchestrator(node=node)
        self.ceph_cluster = node.cluster
        self.client = node.cluster.get_nodes(role="client")[0]
        self._cluster_cache = {}

    # ------------------------------------------------------------------
    # 1. Daemon Discovery
    # ------------------------------------------------------------------

    def discover_nfs_clusters(self, cluster_names):
        """Fetch and cache full NFS cluster topology for all clusters.

        Queries ``ceph orch ps``, ``ceph nfs cluster info``, and
        ``ceph orch ls nfs --export`` once, then caches the results
        in ``self._cluster_cache`` keyed by cluster name.

        Returns the cache dict.  Each entry contains::

            {
                "cluster_name": str,
                "daemons": [{"daemon_name", "daemon_id", "hostname",
                             "host_node", "status", "ports", "rank"}],
                "backends": [{"hostname", "ip", "port"}],
                "virtual_ip": str or None,
                "nfs_port": int,
                "daemon_ips": [str],
                "daemon_count": int,
            }
        """
        log.info(f"Discovering NFS cluster topology for {cluster_names}...")
        all_nodes = self.ceph_cluster.get_nodes()

        for cn in cluster_names:
            entry = {
                "cluster_name": cn,
                "daemons": [],
                "backends": [],
                "virtual_ip": None,
                "nfs_port": 2049,
                "daemon_ips": [],
                "daemon_count": 0,
            }

            # Daemon details from orch ps
            cmd = f"ceph orch ps --service_name nfs.{cn} --format json"
            orch_out = self.rados_obj.run_ceph_command(cmd=cmd)
            for d in orch_out or []:
                hostname = d.get("hostname", "")
                host_node = None
                for n in all_nodes:
                    if n.hostname == hostname or n.shortname == hostname:
                        host_node = n
                        break
                entry["daemons"].append(
                    {
                        "daemon_name": d.get("daemon_name", ""),
                        "daemon_id": d.get("daemon_id", ""),
                        "hostname": hostname,
                        "host_node": host_node,
                        "status": d.get("status_desc", "unknown"),
                        "ports": d.get("ports", []),
                        "rank": d.get("rank"),
                    }
                )

            # Cluster info (backends, VIP, port)
            try:
                info = Ceph(self.client).nfs.cluster.info(cn)
                cluster_info = info.get(cn, {})
                entry["backends"] = cluster_info.get("backend", [])
                entry["virtual_ip"] = cluster_info.get("virtual_ip")
                if entry["backends"]:
                    entry["nfs_port"] = entry["backends"][0].get("port", 2049)
            except Exception as e:
                log.warning(f"  {cn}: cluster info failed: {e}")

            # Derive daemon IPs from running daemons
            running = [
                d
                for d in entry["daemons"]
                if d["status"] == "running" and d["host_node"]
            ]
            entry["daemon_ips"] = [d["host_node"].ip_address for d in running]
            entry["daemon_count"] = len(running)

            self._cluster_cache[cn] = entry

            log.info(
                f"  {cn}: {entry['daemon_count']} daemon(s), "
                f"port={entry['nfs_port']}, "
                f"vip={entry['virtual_ip']}, "
                f"ips={entry['daemon_ips']}"
            )
            for d in entry["daemons"]:
                log.info(
                    f"    {d['daemon_name']} on {d['hostname']} "
                    f"[{d['status']}] ports={d['ports']} "
                    f"rank={d['rank']}"
                )

        return self._cluster_cache

    def get_cluster_info(self, cluster_name):
        """Return cached cluster info, or fetch if not cached."""
        if cluster_name not in self._cluster_cache:
            self.discover_nfs_clusters([cluster_name])
        return self._cluster_cache.get(cluster_name, {})

    def validate_3az_topology(self, cluster_names):
        """Verify each NFS cluster has daemons across multiple DCs.

        Returns True if all clusters have daemons on hosts in at
        least 2 different DCs.  Logs warnings for clusters that
        don't meet the requirement.
        """
        all_ok = True
        for cn in cluster_names:
            info = self.get_cluster_info(cn)
            daemons = info.get("daemons", [])
            running = [d for d in daemons if d["status"] == "running"]
            if len(running) < 2:
                log.warning(
                    f"  {cn}: only {len(running)} running daemon(s), "
                    f"need >= 2 for failover"
                )
                all_ok = False
                continue

            hostnames = {d["hostname"] for d in running}
            log.info(f"  {cn}: {len(running)} daemons across " f"hosts {hostnames}")
        return all_ok

    def get_nfs_daemon_hosts(self, cluster_name):
        """Return NFS daemon info for *cluster_name*.

        Uses cached data from ``discover_nfs_clusters`` if available,
        otherwise fetches fresh.

        Returns a list of dicts::

            [{"daemon_name": str, "daemon_id": str, "hostname": str,
              "host_node": CephNode, "status": str,
              "ports": list, "rank": int}, ...]
        """
        info = self.get_cluster_info(cluster_name)
        if info and info.get("daemons"):
            return info["daemons"]

        cmd = f"ceph orch ps --service_name nfs.{cluster_name} " f"--format json"
        out = self.rados_obj.run_ceph_command(cmd=cmd)
        if not out:
            log.warning(f"No daemons found for nfs.{cluster_name}")
            return []

        all_nodes = self.ceph_cluster.get_nodes()
        results = []
        for entry in out:
            hostname = entry.get("hostname", "")
            host_node = None
            for n in all_nodes:
                if n.hostname == hostname or n.shortname == hostname:
                    host_node = n
                    break
            results.append(
                {
                    "daemon_name": entry.get("daemon_name", ""),
                    "daemon_id": entry.get("daemon_id", ""),
                    "hostname": hostname,
                    "host_node": host_node,
                    "status": entry.get("status_desc", "unknown"),
                    "ports": entry.get("ports", []),
                    "rank": entry.get("rank"),
                }
            )
        return results

    def get_active_mds(self, fs_name):
        """Find the rank-0 active MDS for *fs_name*.

        Returns ``{"daemon_id": str, "hostname": str, "host_node": CephNode}``
        or *None* if no active MDS is found.
        """
        fs_data = self._get_fs_status(fs_name)
        mds_host_map = self._get_mds_host_map()
        for mds in fs_data.get("mdsmap", []):
            if mds.get("rank", -1) == 0 and mds.get("state") == "active":
                name = mds.get("name", "")
                hostname = mds_host_map.get(name, "")
                host_node = (
                    self.rados_obj.get_host_object(hostname) if hostname else None
                )
                return {
                    "daemon_id": name,
                    "hostname": hostname or name,
                    "host_node": host_node,
                }
        return None

    def get_standby_mds(self, fs_name):
        """Return list of standby MDS daemons for *fs_name*."""
        fs_data = self._get_fs_status(fs_name)
        mds_host_map = self._get_mds_host_map()
        standbys = []
        for mds in fs_data.get("mdsmap", []):
            if mds.get("state") == "standby":
                name = mds.get("name", "")
                hostname = mds_host_map.get(name, "")
                standbys.append(
                    {
                        "daemon_id": name,
                        "hostname": hostname or name,
                        "host_node": (
                            self.rados_obj.get_host_object(hostname)
                            if hostname
                            else None
                        ),
                    }
                )
        return standbys

    def _get_fs_status(self, fs_name):
        """Return parsed ``ceph fs status`` JSON for *fs_name*."""
        out, _ = self.client.exec_command(
            sudo=True,
            cmd=f"ceph fs status {fs_name} --format json",
            timeout=60,
        )
        return json.loads(str(out).strip())

    def _get_mds_host_map(self):
        """Return ``{daemon_name: hostname}`` for all MDS daemons.

        Single ``ceph orch ps`` call, reusable across active/standby
        lookups.
        """
        try:
            cmd = "ceph orch ps --daemon_type mds --format json"
            out = self.rados_obj.run_ceph_command(cmd=cmd)
            return {
                entry.get("daemon_id", ""): entry.get("hostname", "")
                for entry in (out or [])
            }
        except Exception as e:
            log.debug(f"  Could not build MDS host map: {e}")
            return {}

    def get_dc_hosts(self, dc_name):
        """Return node objects for all hosts in datacenter *dc_name*.

        Uses ``ceph osd tree`` to find hosts under the datacenter bucket,
        then matches to cluster node objects.
        """
        osd_tree = self.rados_obj.run_ceph_command(cmd="ceph osd tree")
        dc_id = None
        for node_entry in osd_tree.get("nodes", []):
            if (
                node_entry.get("type") == "datacenter"
                and node_entry.get("name") == dc_name
            ):
                dc_id = node_entry.get("id")
                break

        if dc_id is None:
            log.warning(f"Datacenter '{dc_name}' not found in osd tree")
            return []

        dc_node = next(n for n in osd_tree["nodes"] if n.get("id") == dc_id)
        host_ids = dc_node.get("children", [])

        host_names = []
        for node_entry in osd_tree.get("nodes", []):
            if node_entry.get("id") in host_ids:
                if node_entry.get("type") == "host":
                    host_names.append(node_entry["name"])
                else:
                    for child_id in node_entry.get("children", []):
                        child = next(
                            (
                                n
                                for n in osd_tree["nodes"]
                                if n.get("id") == child_id and n.get("type") == "host"
                            ),
                            None,
                        )
                        if child:
                            host_names.append(child["name"])

        all_nodes = self.ceph_cluster.get_nodes()
        return [
            n
            for n in all_nodes
            if n.hostname in host_names or n.shortname in host_names
        ]

    # ------------------------------------------------------------------
    # 2. NFS Daemon Failure Injection
    # ------------------------------------------------------------------

    def kill_nfs_daemon(self, host_node, daemon_name=None, method="orch_stop"):
        """Stop an NFS-Ganesha daemon and trigger cephadm recovery.

        Supports three methods:

        ``orch_stop`` (default)
            ``ceph orch daemon stop <name> --force`` followed by
            ``ceph orch daemon start <name>``.
            The stop --force brings the daemon down; the start
            triggers cephadm to redeploy it.
        ``systemctl``
            ``systemctl stop <unit>`` on the daemon host.
            Cephadm detects the stopped daemon and restarts it.
        ``pkill``
            Sends SIGTERM via ``pkill ganesha.nfsd`` on the host.
            Simulates an unexpected daemon crash (Denali Tier 1).
            Cephadm detects the missing process and restarts it.

        For ``orch_stop`` and ``systemctl``, *daemon_name* is required
        (the full orch daemon name from ``get_nfs_daemon_hosts``).
        """
        log.info(f"Stopping NFS-Ganesha on {host_node.hostname} " f"via {method}...")
        if method == "orch_stop":
            if not daemon_name:
                raise OperationFailedError("daemon_name required for orch_stop method")
            self.client.exec_command(
                sudo=True,
                cmd=f"ceph orch daemon stop {daemon_name} --force",
                timeout=60,
            )
            log.info("  Daemon stopped, issuing start for recovery...")
            self.client.exec_command(
                sudo=True,
                cmd=f"ceph orch daemon start {daemon_name}",
                timeout=60,
            )
        elif method == "systemctl":
            if not daemon_name:
                raise OperationFailedError("daemon_name required for systemctl method")
            fsid = self.rados_obj.run_ceph_command(cmd="ceph fsid")["fsid"]
            unit = f"ceph-{fsid}@{daemon_name}.service"
            log.info(f"  systemctl stop {unit}")
            host_node.exec_command(
                sudo=True,
                cmd=f"systemctl stop {unit}",
                timeout=60,
            )
        else:
            host_node.exec_command(sudo=True, cmd="pkill ganesha.nfsd || true")
        log.info(f"  {method} issued on {host_node.hostname}")

    def stop_nfs_daemon(self, daemon_name):
        """Stop a specific NFS daemon via ``ceph orch daemon stop``.

        *daemon_name* should be the full orch daemon name as returned
        by ``get_nfs_daemon_hosts`` (e.g.,
        ``nfs.nfs-rdma-without-vip.1.0.grim031.lcvgem``).
        """
        log.info(f"Stopping NFS daemon {daemon_name}...")
        self.client.exec_command(
            sudo=True,
            cmd=f"ceph orch daemon stop {daemon_name}",
            timeout=60,
        )

    def restart_nfs_cluster(self, cluster_name, timeout=300):
        """Restart all NFS daemons in *cluster_name* via orch and wait.

        Returns True if all daemons are running within *timeout*.
        """
        log.info(f"Restarting nfs.{cluster_name} via ceph orch...")
        self.client.exec_command(
            sudo=True,
            cmd=f"ceph orch restart nfs.{cluster_name}",
            timeout=60,
        )
        return self._wait_for_nfs_daemons_running(cluster_name, timeout)

    def redeploy_nfs_cluster(self, cluster_name, timeout=600):
        """Redeploy all NFS daemons in *cluster_name* and wait."""
        log.info(f"Redeploying nfs.{cluster_name}...")
        self.client.exec_command(
            sudo=True,
            cmd=f"ceph orch redeploy nfs.{cluster_name}",
            timeout=60,
        )
        return self._wait_for_nfs_daemons_running(cluster_name, timeout)

    def _wait_for_nfs_daemons_running(self, cluster_name, timeout=300):
        """Poll until all NFS daemons for *cluster_name* show running.

        Invalidates the cache on each poll so we get fresh orch data.
        """
        for w in WaitUntil(timeout=timeout, interval=15):
            self._cluster_cache.pop(cluster_name, None)
            daemons = self.get_nfs_daemon_hosts(cluster_name)
            running = [d for d in daemons if d["status"] == "running"]
            log.info(f"  nfs.{cluster_name}: {len(running)}/{len(daemons)} " f"running")
            if len(running) == len(daemons) and len(daemons) > 0:
                return True
        if w.expired:
            log.error(
                f"Not all nfs.{cluster_name} daemons running " f"within {timeout}s"
            )
        return False

    # ------------------------------------------------------------------
    # 3. MDS Failure Injection
    # ------------------------------------------------------------------

    def stop_active_mds(self, fs_name):
        """Stop the active MDS for *fs_name* and return its info.

        Uses ``ceph orch daemon stop`` rather than systemctl because
        MDS daemon IDs (``cephfs.host.suffix``) produce incorrect
        systemd unit names via ``change_daemon_systemctl_state``.

        Returns the dict from ``get_active_mds`` for the stopped daemon,
        or raises ``OperationFailedError`` if no active MDS found.
        """
        active = self.get_active_mds(fs_name)
        if not active:
            raise OperationFailedError(f"No active MDS found for {fs_name}")
        daemon_name = f"mds.{active['daemon_id']}"
        log.info(
            f"Stopping active MDS {daemon_name} on "
            f"{active['hostname']} for {fs_name}..."
        )
        self.client.exec_command(
            sudo=True,
            cmd=f"ceph orch daemon stop {daemon_name}",
            timeout=60,
        )
        return active

    def restart_mds_service(self, fs_name, timeout=300):
        """Restart all MDS daemons for *fs_name* via orch."""
        log.info(f"Restarting mds.{fs_name} via ceph orch...")
        self.client.exec_command(
            sudo=True,
            cmd=f"ceph orch restart mds.{fs_name}",
            timeout=60,
        )
        time.sleep(10)
        for w in WaitUntil(timeout=timeout, interval=15):
            active = self.get_active_mds(fs_name)
            if active:
                log.info(f"  MDS active for {fs_name}: " f"{active['daemon_id']}")
                return True
        if w.expired:
            log.error(f"MDS for {fs_name} did not recover in {timeout}s")
        return False

    def wait_for_mds_failover(self, fs_name, original_active_id, timeout=120):
        """Wait until a different MDS becomes rank-0 active.

        Returns the new active MDS info dict or None on timeout.
        """
        log.info(
            f"Waiting for MDS failover on {fs_name} "
            f"(original: {original_active_id})..."
        )
        for w in WaitUntil(timeout=timeout, interval=5):
            active = self.get_active_mds(fs_name)
            if active and active["daemon_id"] != original_active_id:
                log.info(
                    f"  MDS failover: {original_active_id} -> " f"{active['daemon_id']}"
                )
                return active
        if w.expired:
            log.error(f"MDS failover did not occur within {timeout}s")
        return None

    # ------------------------------------------------------------------
    # 4. Site-Level Failure Simulation
    # ------------------------------------------------------------------

    def power_off_nodes(self, nodes):
        """Power off a list of nodes asynchronously via ``sudo poweroff``.

        Does NOT wait for reconnect -- the nodes are expected to be down.
        """
        log.info(
            f"Powering off {len(nodes)} node(s): " f"{[n.hostname for n in nodes]}..."
        )
        threads = []
        for node in nodes:
            t = Thread(
                target=self._power_off_single,
                args=(node,),
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=30)
        log.info("  Power-off commands issued, waiting 30s...")
        time.sleep(30)

    @staticmethod
    def _power_off_single(node):
        try:
            node.exec_command(
                sudo=True,
                cmd="nohup bash -c 'sleep 2; poweroff' &>/dev/null &",
                check_ec=False,
            )
        except Exception:
            pass

    def power_on_nodes(self, nodes, timeout=600):
        """Reconnect to previously powered-off nodes.

        Polls SSH connectivity in a retry loop. Returns list of nodes
        that successfully reconnected.
        """
        log.info(
            f"Waiting for {len(nodes)} node(s) to come back online "
            f"(max {timeout}s)..."
        )
        recovered = []
        for node in nodes:
            for w in WaitUntil(timeout=timeout, interval=10):
                try:
                    node.reconnect()
                    log.info(f"  {node.hostname}: reconnected")
                    recovered.append(node)
                    break
                except Exception:
                    pass
            if w.expired:
                log.warning(f"  {node.hostname}: not reachable after {timeout}s")
        return recovered

    def reboot_node(self, node, wait_for_cephadm=True):
        """Reboot a single node and optionally wait for cephadm redeploy.

        Uses the validated reboot sequence from manual failover testing.
        """
        log.info(f"Rebooting {node.hostname}...")
        _reboot_node(node)
        if wait_for_cephadm:
            log.info(
                f"  {node.hostname} back, waiting 120s for cephadm "
                f"to redeploy services..."
            )
            time.sleep(120)
        return True

    def install_netsplit_prereqs(self, nodes):
        """Install iptables dependencies on all nodes upfront.

        Should be called once at the start of netsplit scenarios,
        before the split is applied.  Follows the pattern from
        ``test_stretch_netsplit_scenarios.py``.
        """
        log.info(f"Installing iptables prereqs on {len(nodes)} node(s)...")
        for node in nodes:
            try:
                install_package(
                    node=node,
                    packages=["iproute", "net-tools", "iptables-services"],
                )
            except Exception as e:
                log.warning(f"  {node.hostname}: prereq install failed: {e}")

    def netsplit_dc(self, dc_a_hosts, dc_b_hosts):
        """Apply iptables DROP rules between two sets of hosts.

        Uses ``block_in_out_packets_on_host`` from dc_a -> dc_b only.
        That method applies both INPUT and OUTPUT rules on the source
        host, so bidirectional blocking is achieved with one call per
        (source, target) pair.  Follows the proven pattern from
        ``test_stretch_netsplit_scenarios.py``.
        """
        log.info(
            f"Creating netsplit between "
            f"{[n.hostname for n in dc_a_hosts]} and "
            f"{[n.hostname for n in dc_b_hosts]}..."
        )
        for host_a in dc_a_hosts:
            for host_b in dc_b_hosts:
                if not self.rados_obj.block_in_out_packets_on_host(
                    source_host=host_b, target_host=host_a
                ):
                    log.warning(
                        f"  Failed to block {host_a.hostname} " f"on {host_b.hostname}"
                    )
        log.info("  Netsplit applied")

    def restore_netsplit(self, dc_a_hosts, dc_b_hosts):
        """Remove iptables rules and reboot all affected hosts.

        Flushes all iptables chains then reboots each node to
        ensure clean network state.  Follows the recovery pattern
        from ``test_stretch_netsplit_scenarios.py``.
        """
        all_hosts = list(set(dc_a_hosts + dc_b_hosts))
        log.info(f"Restoring connectivity for " f"{[n.hostname for n in all_hosts]}...")
        for node in all_hosts:
            try:
                node.exec_command(
                    sudo=True,
                    cmd="iptables -F",
                    timeout=30,
                    long_running=True,
                )
                log.info(f"  {node.hostname}: iptables flushed")
                node.exec_command(sudo=True, cmd="reboot", check_ec=False)
            except Exception as e:
                log.warning(f"  {node.hostname}: restore failed: {e}")
            time.sleep(20)
        log.info("  Waiting 30s for nodes to reboot...")
        time.sleep(30)

        for node in all_hosts:
            try:
                node.reconnect()
                log.info(f"  {node.hostname}: reconnected")
            except Exception as e:
                log.warning(f"  {node.hostname}: reconnect failed: {e}")

    def isolate_dc(self, dc_hosts, all_other_hosts):
        """Block *dc_hosts* from all *all_other_hosts*."""
        self.netsplit_dc(dc_hosts, all_other_hosts)

    # ------------------------------------------------------------------
    # 5. OSD Failure Injection
    # ------------------------------------------------------------------

    def stop_osd(self, osd_id):
        """Stop a single OSD via systemctl."""
        return self.rados_obj.change_daemon_systemctl_state(
            action="stop", daemon_type="osd", daemon_id=str(osd_id)
        )

    def start_osd(self, osd_id):
        """Start a single OSD via systemctl."""
        return self.rados_obj.change_daemon_systemctl_state(
            action="start", daemon_type="osd", daemon_id=str(osd_id)
        )

    def get_osds_on_host(self, hostname):
        """Return list of OSD IDs on *hostname*."""
        osd_tree = self.rados_obj.run_ceph_command(cmd="ceph osd tree")
        for node_entry in osd_tree.get("nodes", []):
            if node_entry.get("type") == "host" and node_entry.get("name") == hostname:
                return [c for c in node_entry.get("children", []) if isinstance(c, int)]
        return []

    # ------------------------------------------------------------------
    # 6. IO Verification
    # ------------------------------------------------------------------

    def run_fio_on_mounts(self, assignments, fio_params):
        """Run concurrent fio write+read across all mount assignments.

        Returns list of result dicts with write_mbps, read_mbps,
        write_iops, read_iops per mount.
        """
        log.info(
            f"Running fio on {len(assignments)} mount(s) "
            f"(bs={fio_params.get('bs', '1M')}, "
            f"runtime={fio_params.get('runtime', '30')}s)..."
        )
        with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
            futures = [
                executor.submit(
                    self._fio_single_mount,
                    a["client"],
                    a["mount_point"],
                    fio_params,
                )
                for a in assignments
            ]
            results = []
            for i, f in enumerate(futures):
                try:
                    res = f.result()
                    results.append(res)
                except Exception as e:
                    log.warning(
                        f"  [{i}] fio failed on "
                        f"{assignments[i]['mount_point']}: {e}"
                    )
                    results.append(
                        {
                            "write_mbps": 0,
                            "read_mbps": 0,
                            "write_iops": 0,
                            "read_iops": 0,
                            "error": str(e),
                        }
                    )
        return results

    def verify_io_continuity(self, assignments, fio_params):
        """Run fio and return (pass_count, fail_count)."""
        results = self.run_fio_on_mounts(assignments, fio_params)
        passes = sum(1 for r in results if "error" not in r)
        fails = sum(1 for r in results if "error" in r)
        log.info(
            f"  IO continuity: {passes} passed, {fails} failed "
            f"out of {len(results)}"
        )
        return passes, fails

    def run_fio_background(self, assignments, fio_params):
        """Start fio in background, return (executor, futures).

        Caller can inject failures while IO is running, then collect
        results via ``[f.result() for f in futures]``.
        """
        log.info(f"Starting background fio on {len(assignments)} mount(s)...")
        executor = ThreadPoolExecutor(max_workers=len(assignments))
        futures = [
            executor.submit(
                self._fio_single_mount,
                a["client"],
                a["mount_point"],
                fio_params,
            )
            for a in assignments
        ]
        return executor, futures

    def verify_data_integrity(self, assignments, fio_params):
        """Run fio with ``--verify=crc32c`` concurrently and return True if clean."""
        log.info(
            f"Running data integrity verification on " f"{len(assignments)} mount(s)..."
        )
        with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
            futures = {
                executor.submit(self._integrity_single, a, fio_params): a
                for a in assignments
            }
            all_ok = True
            for fut in futures:
                a = futures[fut]
                try:
                    ok = fut.result()
                    if not ok:
                        all_ok = False
                except Exception as e:
                    log.error(f"  {a['mount_point']}: integrity exception: {e}")
                    all_ok = False
        return all_ok

    @staticmethod
    def _integrity_single(assignment, fio_params):
        """Run fio verify on a single mount, return True if clean."""
        a = assignment
        fio_timeout = max(int(fio_params.get("runtime", 30)) + 120, 300)
        cmd = (
            f"fio --name=integrity_test "
            f"--directory={a['mount_point']} "
            f"--rw=write --bs={fio_params.get('bs', '1M')} "
            f"--size={fio_params.get('size', '64M')} "
            f"--verify=crc32c --do_verify=1 "
            f"--direct=1 --ioengine=libaio "
            f"--output-format=json --fallocate=none"
        )
        try:
            out, _ = a["client"].exec_command(sudo=True, cmd=cmd, timeout=fio_timeout)
            data = json.loads(str(out).strip())
            errors = data.get("jobs", [{}])[0].get("verify_errors", 0)
            if errors:
                log.error(f"  {a['mount_point']}: {errors} verify error(s)")
                return False
            log.info(f"  {a['mount_point']}: integrity OK")
            return True
        except Exception as e:
            log.error(f"  {a['mount_point']}: integrity check failed: {e}")
            return False
        finally:
            try:
                a["client"].exec_command(
                    sudo=True,
                    cmd=f"rm -f {a['mount_point']}/integrity_test.*",
                    timeout=30,
                )
            except Exception:
                pass

    @staticmethod
    def _fio_single_mount(client, mount_point, fio_params):
        """Run sequential fio write then read on a single mount."""
        fio_timeout = max(int(fio_params.get("runtime", 30)) + 120, 300)
        base_args = (
            f"--directory={mount_point} "
            f"--bs={fio_params.get('bs', '1M')} "
            f"--iodepth={fio_params.get('iodepth', '16')} "
            f"--numjobs={fio_params.get('numjobs', '2')} "
            f"--size={fio_params.get('size', '64M')} "
            f"--runtime={fio_params.get('runtime', '30')} "
            f"--time_based --direct=1 --ioengine=libaio "
            f"--group_reporting --output-format=json "
            f"--fallocate=none"
        )

        write_cmd = f"fio --name=failover_io --rw=write {base_args}"
        w_out, _ = client.exec_command(sudo=True, cmd=write_cmd, timeout=fio_timeout)
        w_data = json.loads(str(w_out).strip())
        w_bw = w_data["jobs"][0]["write"]["bw"] / 1024.0
        w_iops = w_data["jobs"][0]["write"]["iops"]

        client.exec_command(sudo=True, cmd="echo 3 > /proc/sys/vm/drop_caches")

        read_cmd = f"fio --name=failover_io --rw=read {base_args}"
        r_out, _ = client.exec_command(sudo=True, cmd=read_cmd, timeout=fio_timeout)
        r_data = json.loads(str(r_out).strip())
        r_bw = r_data["jobs"][0]["read"]["bw"] / 1024.0
        r_iops = r_data["jobs"][0]["read"]["iops"]

        try:
            client.exec_command(
                sudo=True,
                cmd=f"rm -f {mount_point}/failover_io.*",
                timeout=30,
            )
        except Exception:
            pass

        return {
            "write_mbps": w_bw,
            "read_mbps": r_bw,
            "write_iops": w_iops,
            "read_iops": r_iops,
        }

    # ------------------------------------------------------------------
    # 7. Cluster Health and Recovery
    # ------------------------------------------------------------------

    def wait_for_cluster_healthy(self, timeout=600):
        """Poll ``ceph health`` until HEALTH_OK or timeout."""
        log.info(f"Waiting for cluster healthy (max {timeout}s)...")
        for w in WaitUntil(timeout=timeout, interval=15):
            try:
                out, _ = self.client.exec_command(
                    sudo=True, cmd="ceph health", timeout=30
                )
                health = str(out).strip()
                if "HEALTH_OK" in health:
                    log.info("  Cluster is HEALTH_OK")
                    return True
                log.info(f"  Health: {health}")
            except Exception as e:
                log.debug(f"  Health check error: {e}")
        if w.expired:
            log.warning(f"Cluster not healthy after {timeout}s")
        return False

    def wait_for_pgs_active_clean(self, timeout=600):
        """Poll PG status until all PGs are active+clean."""
        log.info(f"Waiting for PGs active+clean (max {timeout}s)...")
        for w in WaitUntil(timeout=timeout, interval=20):
            try:
                out, _ = self.client.exec_command(
                    sudo=True,
                    cmd="ceph pg stat --format json",
                    timeout=30,
                )
                data = json.loads(str(out).strip())
                states = data.get("pg_summary", {}).get("num_pg_by_state", [])
                total = sum(s.get("num", 0) for s in states)
                clean = sum(
                    s.get("num", 0)
                    for s in states
                    if "active+clean" == s.get("name", "")
                )
                if clean == total and total > 0:
                    log.info(f"  All {total} PGs active+clean")
                    return True
                log.info(f"  PGs: {clean}/{total} active+clean")
            except Exception as e:
                log.debug(f"  PG stat error: {e}")
        if w.expired:
            log.warning(f"PGs not all active+clean after {timeout}s")
        return False

    def wait_for_stretch_recovery(self, timeout=600):
        """Wait for ``degraded_stretch_mode`` to clear."""
        log.info(f"Waiting for stretch recovery (max {timeout}s)...")
        for w in WaitUntil(timeout=timeout, interval=20):
            try:
                out, _ = self.client.exec_command(
                    sudo=True,
                    cmd="ceph osd dump --format json",
                    timeout=30,
                )
                data = json.loads(str(out).strip())
                if not data.get("stretch_mode", {}).get("stretch_mode_enabled", False):
                    log.info("  Stretch mode not enabled (nothing to wait for)")
                    return True
                health, _ = self.client.exec_command(
                    sudo=True, cmd="ceph health detail", timeout=30
                )
                if "DEGRADED_STRETCH_MODE" not in str(health):
                    log.info("  Stretch mode recovered (no longer degraded)")
                    return True
                log.info("  Still in degraded stretch mode...")
            except Exception as e:
                log.debug(f"  Stretch check error: {e}")
        if w.expired:
            log.warning(f"Stretch mode still degraded after {timeout}s")
        return False

    def verify_mon_quorum(self, expected_min=5):
        """Check at least *expected_min* monitors are in quorum."""
        try:
            out = self.rados_obj.run_ceph_command(cmd="ceph quorum_status")
            quorum = out.get("quorum", [])
            in_quorum = len(quorum)
            log.info(
                f"  MON quorum: {in_quorum} monitors " f"(expected >= {expected_min})"
            )
            return in_quorum >= expected_min
        except Exception as e:
            log.error(f"  Could not check quorum: {e}")
            return False

    def verify_stretch_degraded(self):
        """Assert DEGRADED_STRETCH_MODE is in health detail."""
        try:
            out, _ = self.client.exec_command(
                sudo=True, cmd="ceph health detail", timeout=30
            )
            degraded = "DEGRADED_STRETCH_MODE" in str(out)
            log.info(f"  Stretch degraded: {degraded}")
            return degraded
        except Exception as e:
            log.error(f"  Could not check stretch status: {e}")
            return False

    def check_no_crashes(self):
        """Verify no new crashes via ``ceph crash ls-new``."""
        try:
            out = self.rados_obj.run_ceph_command(cmd="ceph crash ls-new")
            if out:
                log.warning(f"  {len(out)} new crash(es) detected")
                return False
            log.info("  No new crashes")
            return True
        except Exception as e:
            log.debug(f"  Crash check: {e}")
            return True

    # ------------------------------------------------------------------
    # 8. NFS Service Validation
    # ------------------------------------------------------------------

    def verify_nfs_cluster_health(self, cluster_names):
        """Check all NFS daemons running and cluster info valid.

        Invalidates cache to get fresh status after failover events.
        """
        all_healthy = True
        for cn in cluster_names:
            self._cluster_cache.pop(cn, None)
            daemons = self.get_nfs_daemon_hosts(cn)
            running = [d for d in daemons if d["status"] == "running"]
            log.info(f"  nfs.{cn}: {len(running)}/{len(daemons)} " f"daemon(s) running")
            if len(running) != len(daemons):
                all_healthy = False

            try:
                info = Ceph(self.client).nfs.cluster.info(cn)
                backends = info.get(cn, {}).get("backend", [])
                log.info(f"  nfs.{cn}: {len(backends)} backend(s)")
                if not backends:
                    all_healthy = False
            except Exception as e:
                log.warning(f"  nfs.{cn}: cluster info failed: {e}")
                all_healthy = False
        return all_healthy

    def verify_exports_accessible(self, assignments):
        """stat -f each mount point, return (accessible, inaccessible)."""
        accessible = 0
        inaccessible = 0
        for a in assignments:
            if self.check_mount_accessible(a["client"], a["mount_point"]):
                accessible += 1
            else:
                inaccessible += 1
        log.info(f"  Mounts: {accessible} accessible, " f"{inaccessible} inaccessible")
        return accessible, inaccessible

    def check_mount_accessible(self, client, mount_point, timeout=15):
        """Check if a mount is accessible via ``stat -f`` with timeout."""
        try:
            client.exec_command(
                sudo=True,
                cmd=f"timeout {timeout} stat -f -c '%T' {mount_point}",
                timeout=timeout + 10,
            )
            return True
        except Exception:
            return False

    def verify_cross_daemon_visibility(self, assignments):
        """Write via one daemon, temporarily mount from another daemon, verify.

        Picks the first assignment, writes a canary file, then temporarily
        mounts the same export from a different NFS daemon in the same
        cluster to verify the data is visible cross-daemon.  The temporary
        mount is cleaned up after the check.

        Falls back to a simple write+read on the existing mount if only
        one daemon is available for the cluster.
        """
        if not assignments:
            log.warning("  No assignments for cross-daemon check")
            return True

        a1 = assignments[0]
        cluster_name = a1["cluster_name"]

        daemons = self.get_nfs_daemon_hosts(cluster_name)
        other_daemons = [
            d
            for d in daemons
            if d["host_node"]
            and d["host_node"].ip_address != a1["nfs_server_ip"]
            and d["status"] == "running"
        ]

        if not other_daemons:
            log.info(
                "  Cross-daemon: only one daemon available, "
                "skipping cross-daemon check"
            )
            return True

        alt_ip = other_daemons[0]["host_node"].ip_address
        tmp_mount = f"/tmp/cross_daemon_check_{int(time.time())}"
        canary_data = f"xdaemon-{time.time()}"
        canary_name = f"canary_{int(time.time())}"
        canary_path = f"{a1['mount_point']}/{canary_name}"

        log.info(
            f"  Cross-daemon: writing via {a1['nfs_server_ip']}, "
            f"reading via {alt_ip} (tmp mount {tmp_mount})..."
        )

        a1["client"].exec_command(
            sudo=True,
            cmd=f"echo '{canary_data}' > {canary_path}",
            timeout=30,
        )

        ok = False
        try:
            a1["client"].exec_command(
                sudo=True, cmd=f"mkdir -p {tmp_mount}", timeout=10
            )
            a1["client"].exec_command(
                sudo=True,
                cmd=(
                    f"mount -t nfs -o vers=4.2,port={a1['port']} "
                    f"{alt_ip}:{a1['export_path']} {tmp_mount}"
                ),
                timeout=60,
            )
            a1["client"].exec_command(
                sudo=True, cmd="echo 3 > /proc/sys/vm/drop_caches"
            )
            time.sleep(3)

            out, _ = a1["client"].exec_command(
                sudo=True,
                cmd=f"timeout 30 cat {tmp_mount}/{canary_name}",
                timeout=45,
            )
            if canary_data in str(out):
                log.info("  Cross-daemon visibility: OK")
                ok = True
            else:
                log.error(
                    f"  Cross-daemon visibility: MISMATCH "
                    f"(expected '{canary_data}', got '{out}')"
                )
        except Exception as e:
            log.error(f"  Cross-daemon visibility failed: {e}")
        finally:
            try:
                a1["client"].exec_command(
                    sudo=True, cmd=f"umount -l {tmp_mount}", timeout=15
                )
                a1["client"].exec_command(
                    sudo=True, cmd=f"rmdir {tmp_mount}", timeout=10
                )
            except Exception:
                pass
            try:
                a1["client"].exec_command(
                    sudo=True, cmd=f"rm -f {canary_path}", timeout=15
                )
            except Exception:
                pass
        return ok

    def verify_nfs_daemon_count(self, cluster_name, expected_count):
        """Assert the expected number of running NFS daemons."""
        self._cluster_cache.pop(cluster_name, None)
        daemons = self.get_nfs_daemon_hosts(cluster_name)
        running = [d for d in daemons if d["status"] == "running"]
        ok = len(running) == expected_count
        log.info(
            f"  nfs.{cluster_name}: {len(running)} running "
            f"(expected {expected_count}) -> "
            f"{'OK' if ok else 'MISMATCH'}"
        )
        return ok

    def log_nfs_diagnostics(self, cluster_names):
        """Log comprehensive NFS diagnostics for each cluster."""
        log.info("=" * 60)
        log.info("  NFS DIAGNOSTICS (failover)")
        log.info("=" * 60)
        for cn in cluster_names:
            cmds = [
                (f"ceph nfs cluster info {cn}", f"cluster info ({cn})"),
                (
                    f"ceph orch ps --service_name nfs.{cn}",
                    f"daemons ({cn})",
                ),
                (f"ceph nfs export ls {cn}", f"exports ({cn})"),
            ]
            for cmd, label in cmds:
                try:
                    out, _ = self.client.exec_command(sudo=True, cmd=cmd, timeout=60)
                    log.info(f"\n  {label}:\n{out}")
                except Exception as e:
                    log.debug(f"  Could not run '{cmd}': {e}")
        log.info("=" * 60)
