import collections
import concurrent.futures as cf
import json
import logging
import re

import boto
import boto.s3.connection

logger = logging.getLogger(__name__)
log = logger


def run(ceph_cluster, **kw):
    """
    Args:
        ceph_cluster (ceph.ceph.Ceph): ceph cluster
        kw: Args that need to be passed to the test for initialization

    Returns:
        1 -> Fail, 0 -> Pass
    """
    log.info("Running exec test")
    runner = RadosBenchExecutor(ceph_cluster, **kw)
    runner.run()


def count(func):
    """
    Decorator method to check how many times a particular method has been invoked
    :param func: name of the function
    :return: wrapped method
    """

    def wrapped(*args, **kwargs):
        wrapped.calls += 1
        return func(*args, **kwargs)

    wrapped.calls = 0
    return wrapped


class RgwIoTools:
    """
    This class implements the methods required to trigger the Object IO for RGW
    """

    def __init__(self, node, **kwargs):
        """
        Initializing the connection for the objects
        """

        self.node = node
        out, err = self.node.exec_command(cmd="uuidgen")
        self.unique_id = out.read().strip().decode()[0:5]
        self.config = kwargs

        # todo : implement to get radosGW daemon IP
        # rgw_nodes = kwargs.get("rgw_nodes")
        # self.host = rgw_nodes[0].hostname
        self.port = 80
        hosts = self.config["rgw_nodes"]
        self.host = hosts[0].hostname
        if self.config["create_rgw_user"]:
            log.debug(
                "User creation is set to true, creating a radosgw admin user with keys"
            )
            user = f"operator_{self.unique_id}"
            disp_name = f"s3 {user}"
            email = f"{user}@example.com"
            self.access_key = self.unique_id
            self.secret_key = f"{self.unique_id}0000"

            admin_create_command = f"""radosgw-admin user create --uid="{user}" --display-name="{disp_name}" \
--email="{email}" --access_key="{self.access_key}" --secret="{self.secret_key}" """
            self.node.exec_command(cmd=admin_create_command, sudo=True)
            log.info(f"admin user for RGW : {user} created successfully")
        else:
            log.debug(
                "User creation is set to false, creating a radosgw admin user provided with keys"
            )
            self.access_key = self.config["access_key"]
            self.secret_key = self.config["secret_key"]

        try:
            self.conn = boto.connect_s3(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                host=self.host,
                port=self.port,
                is_secure=False,  # comment if you are using ssl
                calling_format=boto.s3.connection.OrdinaryCallingFormat(),
            )
        except AttributeError:
            log.error(
                "Please enter the access key and Secret key as string. Error message"
            )
        except Exception:
            log.error("An exception occurred during connecting with S3 .")
        log.debug(
            "successfully created a connection with the Host for IO using BOTO tool"
        )

    def list_buckets(self):
        """
        lists all the buckets created by the given user
        :return: dictionary of all the buckets with the timestamp
        """
        bucket_dictionary = {}
        log.debug("listing all the buckets on the host")
        for bucket in self.conn.get_all_buckets():
            log.info(f"{bucket.name}\t{bucket.creation_date}")
            bucket_dictionary[bucket.name] = bucket.creation_date
        log.debug(f"all the buckets on the host are : {str(bucket_dictionary)}")
        return bucket_dictionary

    def create_buckets(self, quantity):
        """
        Creates the buckets as many as specified in the
        :param quantity: no of buckets to be created
        :return: Returns the list of buckets created
        """
        buckets_list = []
        log.debug("creating buckets for RGW IO")
        for no in range(int(quantity)):
            name = f"my-bucket-{self.unique_id}-no-{no}"
            log.debug(f"creating bucket : {name}")
            try:
                bucket = self.conn.create_bucket(name)
                buckets_list.append(bucket.name)
            except Exception as err:
                log.error(
                    f"An error occurred when creating the bucket {name}. Error message : \n {err}"
                )
        log.debug(f"all the buckets created are : {str(buckets_list)}")
        return buckets_list

    def list_bucket_content(self, bucket=None):
        """
        Lists the content of the bucket.

        When a bucket name is provided, returns the contents of that particular bucket,
        else lists the contents of all the buckets created by the particular user
        :param bucket: Name of the bucket whose contents need to be listed.
        :return: dictionary of the objects with bucket name with key
        """
        objects_dictionary = {}
        bktobjects = collections.namedtuple("bktobjects", ["name", "size", "modified"])
        log.debug("Listing the objects inside the specified bucket(s)")
        if bucket:
            bucket = self.conn.get_bucket(bucket)
            log.debug(f"Indivudial bucket name given. Bucket {bucket.name}")
            key_list = []
            for key in bucket.list():
                log.info(
                    f"bucket : {bucket.name}\t{key.name}\t{key.size}\t{key.last_modified}"
                )
                key_list.append(bktobjects(key.name, key.size, key.last_modified))
            objects_dictionary[bucket.name] = key_list
        else:
            log.debug("listing contents of all the buckets created by user")
            for bucket in self.conn.get_all_buckets():
                # bucket = self.conn.get_bucket(bucket)
                key_list = []
                for key in bucket.list():
                    log.info(
                        f"bucket : {bucket.name}\t{key.name}\t{key.size}\t{key.last_modified}"
                    )
                    key_list.append(bktobjects(key.name, key.size, key.last_modified))
                objects_dictionary[bucket.name] = key_list
        log.debug(f"the objects are : {str(objects_dictionary)}")
        return objects_dictionary

    def create_bucket_object(self, bucket, quantity):
        """
        creates the given number of objects inside the given bucket
        :param bucket: name of the bucket where the object needs to be created
        :param quantity: number of objects to be created
        :return: list of all the keys of objects created
        """
        obj_key_list = []
        log.info(f"creating {quantity} objects inside bucket {bucket}")
        bucket = self.conn.get_bucket(bucket)
        for no in range(int(quantity)):
            ukey = f"obj_{self.unique_id}_no{no}"
            log.debug(f"creating the object no : {no} with key : {ukey}")
            try:
                key = bucket.new_key(ukey)
                copy_string = f"""
                This is a test object being written for the key : {ukey}
                This python script can be used to write IO into the given host.
                """
                key.set_contents_from_string(copy_string)
                obj_key_list.append(ukey)
            except Exception as err:
                log.error(
                    f"An error occurred when creating the object {ukey} in bucket {bucket}."
                    f" Error message : \n {err}"
                )
        log.debug(f"All the keys created are : {str(obj_key_list)}")
        return obj_key_list

    def delete_boto_object(self, bucket, key=None, delete_all=False):
        """
        Deletes the given object from the bucket.

        If the Key is specified, deletes only the object from the bucket, otherwise deletes all the objects from
        the given bucket
        :param bucket: name of the bucket from where the object needs to be deleted
        :param key: name of the key to be deleted.
        :param delete_all: If true, deletes all the objects in the given bucket
        :return: None
        """
        log.info(f"Deleting the object(s) present in the given bucket {bucket}")
        key_list = [
            key,
        ]
        bucket = self.conn.get_bucket(bucket)
        if delete_all:
            log.debug(f"selected to delete all the objects in bucket {bucket.name}")
            key_list_dict = self.list_bucket_content(bucket)
            key_list = [ob.name for ob in key_list_dict[bucket]]
            log.debug(f"the keys obtained for bucket {bucket.name} are : {key_list}")

        for key in key_list:
            try:
                bucket.delete_key(key)
            except Exception as err:
                log.error(
                    f"An error occurred when deleting the object {key} in bucket {bucket.name}."
                    f" Error message : \n {err}"
                )
            log.debug(f"Delete the object {key} in bucket {bucket.name}")
        log.info(f"done with deleting object(s) in bucket {bucket.name}")

    def delete_boto_bucket(self, bucket):
        """
        Deletes the empty bucket. If the bucket is not empty, deletes all the objects and then delets bucket
        :param bucket: Name of the bucket to be deleted.
        :return: None
        """
        bucket = self.conn.get_bucket(bucket)
        log.info(f"Bucket provided to be deleted : {bucket.name}")
        contents = self.list_bucket_content(bucket.name)
        if len(contents[bucket.name]) >= 1:
            log.info(
                f"Bucket {bucket.name} is not empty. Deleting objects before deleting"
            )
            self.delete_boto_object(bucket=bucket.name, delete_all=True)
        try:
            self.conn.delete_bucket(bucket.name)
        except Exception as err:
            log.error(
                f"An error occurred when deleting bucket {bucket.name}."
                f" Error message : \n {err}"
            )
        log.info(f"completed deleting bucket {bucket.name}")

    def download_boto_objects(self, bucket, key=None):
        """
        Used to download the object on to local file system simulating read option.

        If Key is specified along with bucket name, only that object will be downloaded, Otherwise all the objects in
        the bucket will be downloaded. Creates a folder called boto_objects and downloads them in the folder.
        :param bucket: Name of the bucket from where to download a object
        :param key: Name of the object to be downloaded
        :return: None
        """

        bucket = self.conn.get_bucket(bucket)
        # creating a folder for downloading the files
        folder_name = f"object_downloads_{self.unique_id}"
        out, err = self.node.exec_command(cmd=r"ls -ll ~/")
        output = out.read().decode()
        log.info(f"captured file output is {output}")
        if not re.search(folder_name, output):
            folder_create_cmd = f"mkdir {folder_name}"
            log.debug(
                f"Creating the folder : {folder_name} via the command : {folder_create_cmd}"
            )
            self.node.exec_command(cmd=folder_create_cmd, sudo=True)
        log.info(f"Downloading object(s) from the bucket {bucket.name}")
        keys = [
            key,
        ]
        if not key:
            log.debug(f"Downloading all the objects from the bucket {bucket.name}")
            bkt_content = self.list_bucket_content(bucket=bucket.name)
            log.debug(
                f"\n\nDownloading The contents of bucket : {bucket.name}. The list of objects obtained"
                f" is :\n{str(bkt_content)}\n\n and number of objects is/are {len(bkt_content[bucket.name])}"
            )
            keys = [
                bkt_content[bucket.name][cnt].name
                for cnt in range(len(bkt_content[bucket.name]))
            ]
            log.debug(f"All the keys obtained for downloading are : {keys}")

        # Proceeding to download all the keys provided
        keys = [ky for ky in keys if ky[-1] != "/"]
        for key in keys:
            log.debug(f"Downloading the objects {key} from the bucket {bucket.name}")
            # creating a file to download the contents of the object
            file_name = f"object_{bucket.name}_{key}.txt"
            file_create_cmd = f"touch {folder_name}/{file_name}"
            self.node.exec_command(cmd=file_create_cmd, sudo=True)
            log.debug(
                f"the name of the download file is {file_name}, creating file via command : {file_create_cmd}"
            )
            try:
                key = bucket.get_key(key)
                key.get_contents_to_filename(f"{folder_name}/{file_name}")
            except Exception:
                log.error(
                    f"An error occurred when downloading the object {key} in bucket {bucket.name}."
                )

    def generate_boto_obj_url(self, bucket, key=None):
        """
        Used to create download URL for the object simulating read option.

        If Key is specified along with bucket name, only for that object the URL will be generated,
         Otherwise all the objects in the bucket will have the download URL's.
        :param bucket: Name of the bucket from where to download a object
        :param key: Name of the object for which URL should be generated
        :return: Returns the list of objects URL's
        """
        bucket = self.conn.get_bucket(bucket)
        log.info(f"Creating URL's for object(s) from the bucket {bucket.name}")
        all_url = []
        keys = [
            key,
        ]
        if not key:
            log.debug(f"Downloading all the objects from the bucket {bucket.name}")
            bkt_content = self.list_bucket_content(bucket=bucket.name)
            keys = [
                bkt_content[bucket.name][cnt].name
                for cnt in range(len(bkt_content[bucket.name]))
            ]

        # Proceeding to download all the keys provided
        keys = [ky for ky in keys if ky[-1] != "/"]
        for key in keys:
            log.debug(f"Downloading the objects {key} from the bucket {bucket.name}")
            try:
                key_name = bucket.get_key(key)
                obj_url = key_name.generate_url(0, query_auth=False, force_http=True)
                log.debug(
                    f"The URL generated is : {str(obj_url)} of type {type(obj_url)}"
                )
                all_url.append(obj_url)
            except Exception as err:
                log.error(
                    f"An error occurred when generating URI the object {key} in bucket {bucket.name}."
                    f" Error message : \n {err}"
                )
        return all_url


class RadosIoTools:
    """
    This class implements the methods required to trigger the Object IO via Rados Bench tool
    """

    @count
    def __init__(self, node, **kwargs):
        """
        Initializing class object by creating a pool for triggering Rados bench
        """
        self.node = node
        out, err = self.node.exec_command(cmd="uuidgen")
        self.unique_id = out.read().strip().decode()[0:5]
        self.config = kwargs
        self.pool_name = f"instant_io_pool_{self.__init__.calls}_{self.unique_id}"
        pool_create_cmd = f"ceph osd pool create {self.pool_name} 64 64"
        log.debug(
            f"Creating pool : {self.pool_name} using the command : {pool_create_cmd}"
        )
        self.node.exec_command(cmd=pool_create_cmd, sudo=True)

        # Checking if custom crush rule to be applied to pools
        if kwargs.get("crush_rule"):
            crush_cmd = f"ceph osd pool set {self.pool_name} crush_rule {kwargs.get('crush_rule')}"
            self.node.exec_command(cmd=crush_cmd, sudo=True)

        enable_app_cmd = f"ceph osd pool application enable {self.pool_name} rados"
        log.debug(
            f"Enabling rados application on pool : {self.pool_name} using the command : {enable_app_cmd}"
        )
        self.node.exec_command(cmd=enable_app_cmd, sudo=True)

        # todo: checking if the pool creation was successful
        log.info(f"Created pool {self.pool_name} for Rados Bench successfully")

    def bench_write_ops(self, bsize, duration):
        """
        Method to trigger Write operation via the Rados Bench tool
        :param bsize: block size to write
        :param duration: no of seconds to write the bench objects
        :return: None
        """
        # dropping the cache from the system before triggering the test
        cmd = "echo 3 | tee /proc/sys/vm/drop_caches && sync"
        self.node.exec_command(cmd=cmd, sudo=True)
        log.debug("Performing Normal writes.")
        bench_write_cmd = (
            f"rados --no-log-to-stderr -b {int(bsize)} -p {self.pool_name} "
            f"bench {duration} write --no-cleanup"
        )
        op = self.node.exec_command(cmd=bench_write_cmd, sudo=True)
        log.debug(
            f"Performed Write on pool {self.pool_name} using command : {cmd} \n  Output :: \n {op} \n"
        )
        log.info(
            f"finished performing write operation via Rados Bench tool on pool {self.pool_name}"
        )

    def bench_read_ops(self, duration):
        """
        Method to perform sequential and Random reads on using the rados bench tool
        :param duration: no of seconds to read the bench objects
        :return: None
        """
        log.info(f"Performing read operations on the pool {self.pool_name}")
        if self.config["sequential_read"]:
            log.info(
                f"Performing sequental read operation on the pool {self.pool_name}"
            )
            cmd = f"rados --no-log-to-stderr -p {self.pool_name} bench {duration} seq"
            log.debug(
                f"Performing sequential read operations on the pool {self.pool_name} using {cmd}"
            )
            op = self.node.exec_command(cmd=cmd, sudo=True)
            log.debug(
                f"Performed sequential read on pool {self.pool_name} using command :{cmd} \nOutput :: \n{op}\n"
            )
            return

        if self.config["random_read"]:
            log.info(
                f"Performing sequental read operation on the pool {self.pool_name}"
            )
            cmd = f"rados --no-log-to-stderr -p {self.pool_name} bench {duration} rand"
            log.debug(
                f"Performing Random read operations on the pool {self.pool_name} using {cmd}"
            )
            op = self.node.exec_command(cmd=cmd, sudo=True)
            log.debug(
                f"Performed sequential read on pool {self.pool_name} using command :{cmd} \nOutput :: \n{op}\n"
            )
            return

        else:
            log.info("Read operations not specified in the config file... Exiting ....")

    def bench_cleanup(self):
        """
        Removes the data created by the rados bench command
        :return: None
        """
        log.info(f"Deleting the objects created in the pool : {self.pool_name}")
        cmd = f"rados -p {self.pool_name} cleanup"
        op = self.node.exec_command(cmd=cmd, sudo=True)
        log.debug(
            f"Performed cleanup of pool {self.pool_name} using command : {cmd} \n  Output :: \n {op} \n"
        )
        # cmd = f"ceph osd pool delete {self.pool_name} --yes-i-really-really-mean-it"
        # cmdline(cmd)


class RbdFioTools:
    """
    Class containing modules for running File IO for Rados block devices
    """

    @count
    def __init__(self, node, **kwargs):
        """
        Performs all the pre-requsits fro running FIO on for testing.

        Steps performed in init:
        1. Create a pool for testing
        2. Create a rbd image in the test pool
        3. Map image to a block device
        4. Make file system
        5. Mount the Ceph rbd image image
        """
        log.debug("Performing pre-requisites for running FIO on the given host")
        # Create a pool for testing
        self.node = node
        out, err = self.node.exec_command(cmd="uuidgen")
        self.unique_id = out.read().strip().decode()[0:5]
        self.config = kwargs

        # cmd to install the FIO RPM on the given node for running File IO
        out, err = self.node.exec_command(cmd="rpm -qa", sudo=True)
        output = out.read().decode()
        if not re.search("fio", output):
            cmd = "yum install fio -y"
            log.debug(f"Installing the fio rpms using the cmd {cmd}")
            self.node.exec_command(cmd=cmd, sudo=True)

        # Creating a mount directory for mounting RBD images created
        folder_name = r"ceph-block-device"
        out, err = self.node.exec_command(cmd="ls -ll /mnt/", sudo=True)
        output = out.read().decode()
        if not re.search(folder_name, output):
            cmd = "mkdir /mnt/ceph-block-device"
            log.debug(f"Creating a mount directory using the cmd {cmd}")
            self.node.exec_command(cmd=cmd, sudo=True)

        log.info(
            "Completing the pre-reqs of installing the FIO rpm and creating the mount directory"
        )

        self.pool_name = f"rbd_io_pool_{self.__init__.calls}_{self.unique_id}"
        pool_create_cmd = f"ceph osd pool create {self.pool_name} 256 256"
        log.debug(
            f"Creating pool : {self.pool_name} using the command : {pool_create_cmd}"
        )
        self.node.exec_command(cmd=pool_create_cmd, sudo=True)
        # Checking if custom crush rule to be applied to pools
        if kwargs.get("crush_rule"):
            crush_cmd = f"ceph osd pool set {self.pool_name} crush_rule {kwargs.get('crush_rule')}"
            self.node.exec_command(cmd=crush_cmd, sudo=True)
        enable_app_cmd = f"ceph osd pool application enable {self.pool_name} rbd"
        log.debug(
            f"Enabling rbd application on pool : {self.pool_name} using the command : {enable_app_cmd}"
        )
        self.node.exec_command(cmd=enable_app_cmd, sudo=True)

        self.image_name = f"rbd_io_image_{self.__init__.calls}_{self.unique_id}"
        image_create = f"rbd create {self.image_name} --size 4096 --pool {self.pool_name} --image-feature layering"
        log.debug(
            f"Creating image : {self.image_name} using the command : {image_create}"
        )
        self.node.exec_command(cmd=image_create, sudo=True)

        # Checking if client.admin user is present or not. if not importing it from /etc/ceph/ceph.client.admin.keyring
        cmd = r"ceph auth ls -f json-pretty"
        out, err = self.node.exec_command(cmd=cmd, sudo=True)
        output = out.read().decode()
        flag = False
        for entry in json.loads(output)["auth_dump"]:
            if re.search("client.admin", entry["entity"]):
                flag = True
        if not flag:
            cmd = r"ceph auth import -i /etc/ceph/ceph.client.admin.keyring"
            self.node.exec_command(cmd=cmd, sudo=True)
        # Mapping the image create to the client
        image_map_cmd = (
            f"rbd map {self.image_name} --pool {self.pool_name} --name client.admin"
        )
        log.debug(
            f"Mapping image : {self.image_name} to client using the command : {image_map_cmd}"
        )
        self.node.exec_command(cmd=image_map_cmd, sudo=True)

        # Creating file system on the image created
        create_fs_cmd = f"mkfs.ext4 -m0 /dev/rbd/{self.pool_name}/{self.image_name}"
        log.debug(
            f"Creating the File system on the image: {self.image_name} using cmd command : {create_fs_cmd}"
        )
        self.node.exec_command(cmd=create_fs_cmd, sudo=True)

        # Mounting the image on /mnt/ceph-block-device
        mount_image_cmd = (
            f"mount /dev/rbd/{self.pool_name}/{self.image_name} /mnt/ceph-block-device"
        )
        log.debug(
            f"Mounting the image: {self.image_name} using cmd command : {mount_image_cmd}"
        )
        self.node.exec_command(cmd=create_fs_cmd, sudo=True)

        # Performing a small write using rbd-bench
        bench_cmd = f"rbd bench-write {self.image_name} --pool={self.pool_name}"
        log.debug(
            f"Running rbd-bench the image: {self.image_name} using cmd command : {bench_cmd}"
        )
        self.node.exec_command(cmd=bench_cmd, sudo=True)

        # Capturning image details :
        # details_cmd = f"rbd info {self.pool_name}/{self.image_name}"

        # collecting config specified in the JSON file
        self.num_loops = self.config["num_loops"]
        self.num_jobs = self.config["num_parallel_jobs"]
        self.block_size = self.config["block_size"]
        self.write_size = self.config["write_size"]
        self.run_time = self.config["run_time"]
        delete = 0 if self.config["delete_file_data"] else 1

        self.gen_fio_cmd = (
            f"sudo fio --name=global --ioengine=rbd --clientname=admin --pool={self.pool_name}"
            f" --rbdname={self.image_name} --bs={self.block_size} --size={self.write_size}"
            f" --direct=0 --iodepth=32 --runtime={self.run_time} --numjobs={self.num_jobs}"
            f" --loops={self.num_loops} --cgroup_nodelete={delete} --group_reporting "
        )

        log.debug(f"Base command for triggering FIO is : {self.gen_fio_cmd}")

    def fio_write_ops(self):
        """
        Method triggers sequential and Random writes on the given pool.
        """
        log.info(
            f"Performing Random and Sequential write on the image : {self.image_name}"
        )
        fio_write_cmd = f"{self.gen_fio_cmd} --name=seq_write --rw=write --name=rand_write --rw=randwrite"
        try:
            out, err = self.node.exec_command(cmd=fio_write_cmd, sudo=True)
            output = out.read().decode()
            log.debug(
                f"Performed the Write actions. \n Output collected :\n\n {output}\n\n"
            )
        except Exception:
            log.error("Encountered error during fio write operations.")

        # Capturing image details :
        details_cmd = f"rbd info {self.pool_name}/{self.image_name}"
        log.debug(
            f"image details after write operations for: {self.image_name} is : \n "
            f"{self.node.exec_command(cmd=details_cmd)}"
        )

    def fio_read_ops(self):
        """
        Method triggers sequential and Random reads on the given pool.
        """
        log.info(
            f"Performing Random and Sequential reads on the image : {self.image_name}"
        )
        fio_read_cmd = f"{self.gen_fio_cmd} --name=seq_read --rw=read --name=rand_read --rw=randread"
        try:
            out, err = self.node.exec_command(cmd=fio_read_cmd, sudo=True)
            output = out.read().decode()
            log.debug(
                f"Performed the Read actions. \n Output collected :\n\n {output}\n\n"
            )
        except Exception:
            log.error("Encountered error during fio read operations.")

        # Capturning image details :
        details_cmd = f"rbd info {self.pool_name}/{self.image_name}"
        log.debug(
            f"image details after read operations for: {self.image_name} is : \n "
            f"{self.node.exec_command(cmd=details_cmd)}"
        )

    def fio_readwrite_ops(self):
        """
        Method triggers sequential and Random reads on the given pool.
        """
        log.info(
            f"Performing Random and Sequential reads on the image : {self.image_name}"
        )
        fio_read_cmd = f"{self.gen_fio_cmd} --name=seq_readwrite --rw=readwrite --name=rand_readwrite --rw=randrw"
        try:
            out, err = self.node.exec_command(cmd=fio_read_cmd, sudo=True)
            output = out.read().decode()
            log.debug(
                f"Performed the Read & write actions. \n Output collected :\n\n {output}\n\n"
            )
        except Exception:
            log.error("Encountered error during fio Read/Write operations.")

        # Capturning image details :
        details_cmd = f"rbd info {self.pool_name}/{self.image_name}"
        log.debug(
            f"image details after read/write operations for: "
            f"{self.image_name} is : \n {self.node.exec_command(cmd=details_cmd)}"
        )


class SmallFileTools:
    """
    Class containing all the methods required for running IO from small files.
    """

    @count
    def __init__(self, node, **kwargs):
        """
        Initializes the class objects by creating
        1. Checking if cephfs_data and cephfs_metadata pools are present
        2. Creating a mount point for file IO to be run
        3. Mounting the share using client admin keyring
        """
        self.node = node
        out, err = self.node.exec_command(cmd="uuidgen")
        self.unique_id = out.read().strip().decode()[0:5]
        self.config = kwargs
        # 1. Checking if cephfs_data and cephfs_metadata pools are present
        out, err = self.node.exec_command(cmd="ceph osd lspools")
        output = out.read().decode()
        log.debug(f"the op of all the pools are : \n{out}")
        if "cephfs_data" not in output and "cephfs_metadata" not in output:
            pool_create_cmd = r"ceph osd pool create cephfs_data 64 64;"
            self.node.exec_command(cmd=pool_create_cmd, sudo=True)
            pool_create_cmd = r"ceph osd pool create cephfs_metadata 64 64"
            self.node.exec_command(cmd=pool_create_cmd, sudo=True)
            if kwargs.get("crush_rule"):
                crush_cmd = f"ceph osd pool set cephfs_data crush_rule {kwargs.get('crush_rule')}"
                self.node.exec_command(cmd=crush_cmd, sudo=True)
                crush_cmd = f"ceph osd pool set cephfs_metadata crush_rule {kwargs.get('crush_rule')}"
                self.node.exec_command(cmd=crush_cmd, sudo=True)
            enable_app_cmd = r"ceph osd pool application enable cephfs_data cephfs;"
            self.node.exec_command(cmd=enable_app_cmd, sudo=True)
            enable_app_cmd = r"ceph osd pool application enable cephfs_metadata cephfs"
            self.node.exec_command(cmd=enable_app_cmd, sudo=True)

        # 2. Creating a mount point for file IO to be run
        self.mnt_pnt = "mycephfs"
        out, err = node.exec_command(cmd="ls -ll /mnt/")
        output = out.read().decode()
        if not re.search(self.mnt_pnt, output):
            self.node.exec_command(cmd="mkdir /mnt/mycephfs", sudo=True)

        # 3. Mounting the share using client admin keyring if not already mounted
        out, err = self.node.exec_command(cmd="mount -l", sudo=True)
        output = out.read().decode()
        log.debug(f"O/P of the mount command : {output}")
        if not re.search("/mnt/mycephfs type ceph", output):
            cmd = r"mount -t ceph :/ /mnt/mycephfs -o name=admin"
            self.node.exec_command(cmd=cmd, sudo=True)

    def complete_prereqs(self):
        """
        checks the pre-reqs by :
        1. presence MDS daemon. ( atleast 1 active )
        2. Presence of admin keyring in client node at /etc/ceph
        3. Smallfile repo successfully cloned and ready to be used.
        4. checking IS MOUNT HELPER IS PRESENT?
        :return: Returns 1 if every pre-req is satisfied, otherwise returns 0 for fail
        """

        # 1 checking if MDS daemon is up and active
        out, err = self.node.exec_command(cmd="ceph mds stat", sudo=True)
        output = out.read().decode()
        log.debug(f"O/P of command ceph mds stat is : {output}")
        if "up:active" not in output:
            log.error(
                "No up and active MDS server was found to be running on the cluster. Unable to trigger FIO"
            )
            return 0
        log.debug("At least 1 MDS daemon is up and active")

        # 2. checking presence of admin keyring
        out, err = self.node.exec_command(cmd="ls -ll /etc/ceph/")
        output = out.read().decode()
        if "ceph.client.admin.keyring" not in output:
            log.error(
                "No Admin keyring was found on the client node. Won't be able to "
            )
            return 0
        log.debug("Admin Keyring is present in the client node")

        # 3. Smallfile repo successfully cloned and ready to be used.
        out, err = self.node.exec_command(
            cmd="cd /tmp/; python3 smallfile/smallfile_cli.py --help", sudo=True
        )
        output = out.read().decode()
        log.debug(f"ottput of the smallfile help cli is {output}")
        if "usage: smallfile_cli.py" not in output:
            log.error("failed to execute smallfile_cli.py --help... Exiting")
            return 0

        # 4. checking IS MOUNT HELPER IS PRESENT?
        out, err = self.node.exec_command(cmd="stat /sbin/mount.ceph", sudo=True)
        output = out.read().decode()
        log.debug(f"the op of mount helper is: \n{output}")
        if "/sbin/mount.ceph" not in output:
            log.error("Mount helper not present in the client node. Exiting..")
            return 0

        log.info(
            "All pre-req checks for small files for running CephFile IO completed successfully"
        )
        return 1

    def run_file_write_ops(self):
        """
        Method to trigger the write operations for the Ceph file system
        :return: None
        """
        threads = self.config["num_threads"]
        files = self.config["num_files"]
        fsize = self.config["file_size"]

        create_cmd = (
            f"cd /tmp/; python3 smallfile/smallfile_cli.py --operation append --threads {threads} "
            f"--file-size {fsize} --files {files} --top /mnt/{self.mnt_pnt} --prefix {self.unique_id}"
            f" --verify-read Y --response-times Y --output-json test-io-{self.unique_id}"
        )
        try:
            log.info(f"the smallfile write cmd is  : {create_cmd}")
            out, err = self.node.exec_command(cmd=create_cmd, sudo=True)
            output = out.read().decode()
            log.debug(f"The o/p of the file write ops is : {output}")
        except Exception:
            log.error("The error collected from file IO write")

    def run_file_read_ops(self):
        """
        Method to trigger the read operations for the Ceph file system
        :return: None
        """

        #  python smallfile_cli.py --operation read --top /mnt/mycephfs/ --prefix test1
        read_cmd = (
            f"cd /tmp/; python3 smallfile/smallfile_cli.py --operation read"
            f" --top /mnt/{self.mnt_pnt} --prefix {self.unique_id}"
        )
        try:
            log.info(f"the smallfile read cmd is  : {read_cmd}")
            out, err = self.node.exec_command(cmd=read_cmd, sudo=True)
            output = out.read().decode()
            log.debug(f"The o/p of the file read ops is : {output}")
        except Exception:
            log.error("The error collected from file IO read")


def run_rgw_io(node, **kwargs):
    """
    Creates object of class RgwIoTools and runs IO
    :return: None
    """
    rgw_obj = RgwIoTools(node=node, **kwargs)
    # Creating no of buckets specified in the config
    if kwargs["create_bkt_obj"]:
        log.info("Creating new buckets")
        rgw_obj.create_buckets(quantity=kwargs["num_buckets"])

    # Listing all the Newly created buckets
    dict_buckets = rgw_obj.list_buckets()
    bucket_list = [keys for keys in dict_buckets.keys()]
    log.info(
        f"all the buckets Present for the given User are are : {str(bucket_list)}"
    )

    if kwargs["create_bkt_obj"]:
        # creating objects in each bucket as provided in the config file
        user_val = kwargs["avoid_user_created_bkts"]
        if user_val:
            if user_val.upper() == "ALL":
                bucket_li = [bkt for bkt in bucket_list if rgw_obj.unique_id in bkt]
            else:
                ignore_list = [bkt.strip() for bkt in user_val.split(",")]
                bucket_li = [bkt for bkt in bucket_list if bkt not in ignore_list]
            log.info(
                f"The list of buckets after removing the user provided exclude list is :\n{bucket_li}"
            )
            with cf.ThreadPoolExecutor() as exe:
                [
                    exe.submit(
                        rgw_obj.create_bucket_object,
                        bucket=bkt,
                        quantity=kwargs["num_objects"],
                    )
                    for bkt in bucket_li
                ]
        else:
            with cf.ThreadPoolExecutor() as exe:
                [
                    exe.submit(
                        rgw_obj.create_bucket_object,
                        bucket=bkt,
                        quantity=kwargs["num_objects"],
                    )
                    for bkt in bucket_list
                ]

    # # Listing the contents of a single bucket
    # bkt_content_single = rgw_obj.list_bucket_content(bucket=bucket_list[0])
    # log.debug(
    #     f"\n\n\n the contents of single bucket {bucket_list[0]} are \n {bkt_content_single}\n\n"
    # )
    #
    # # # Listing contents of all the buckets created
    # bkt_content_all = rgw_obj.list_bucket_content()
    # log.debug(f"\n\n\n the contents all buckets are  \n {bkt_content_all}\n\n")
    #
    # # Downloading the objects created and placing them in the folder
    # bucket_name = bucket_list[0]
    # bkt_content_single = rgw_obj.list_bucket_content(bucket=bucket_name)
    # # Selecting the 1st object from the bucket to be deleted
    # single_key = bkt_content_single[bucket_name][0].name
    #
    # # command for downloading 1 the object in the given bucket with the key provided
    # rgw_obj.download_boto_objects(bucket=bucket_name, key=single_key)

    # downloading all the objects in all the buckets
    # if kwargs["download_objects"]:
    #     for names in bucket_list:
    #         log.info(f"Downloading objects for bucket : {names}")
    #         rgw_obj.download_boto_objects(bucket=names)
    #         all_uri = rgw_obj.generate_boto_obj_url(bucket=names)
    #         log.debug(f"The URL's generated for bucket {names} are :\n{str(all_uri)}\n")

    # Selecting a single key and deleting a single object by providing object key and the bucket name
    # bucket_name = li[0]
    # Selecting the 1st object from the bucket to be deleted
    # single_key = bkt_content_single[bucket_name][0].name
    # rgw_obj.delete_boto_object(bucket=bucket_name, key=single_key)
    # bkt_content_single = rgw_obj.list_bucket_content(bucket=li[0])
    # log.debug(f"contents of bucket after deleting a single key {single_key} is given below\n{bkt_content_single}")

    # deleting all the objects and the buckets created
    if kwargs["delete_buckets_and_objects"]:
        # deleting buckets and objects only if they have been created by the script, other wise leaving them intact.
        # bucket_list = [bkt for bkt in bucket_list if unique_id in bkt]
        bucket_list = [bkt for bkt in bucket_list]
        for bucket in bucket_list:
            rgw_obj.delete_boto_bucket(bucket)
        list_buckets = rgw_obj.list_buckets()
        log.debug(
            f"\n\n\nAfter deleting all the buckets {str(list_buckets.keys())}\n\n\n"
        )
    log.info("Finished Running RGW IO using BOTO tool")


def run_rados_io(node, **kwargs):
    """
    Creates object of class RadosIoTools and runs IO
    :return: None
    """
    block_size = kwargs["Size"]
    dur_write = kwargs["write_seconds"]
    # dur_read = kwargs["write_seconds"]

    with cf.ThreadPoolExecutor() as exe:
        for i in range(kwargs["no_pools"]):
            name = RadosIoTools(node=node, **kwargs)
            exe.submit(name.bench_write_ops, bsize=block_size, duration=dur_write)
            # name.bench_read_ops(duration=dur_read)

            # Deleting the benckmark objects created
            if kwargs["delete_bench_data"]:
                name.bench_cleanup()


def run_block_io(node, **kwargs):
    """
    Creates object of class RbdFioTools and runs IO
    :return: None
    """

    rbd_obj = RbdFioTools(node=node, **kwargs)
    with cf.ThreadPoolExecutor() as exe:
        exe.submit(rbd_obj.fio_write_ops)
        # rbd_obj.fio_read_ops()
        exe.submit(rbd_obj.fio_readwrite_ops)


def run_file_io(node, **kwargs):
    """
    Creates object of class SmallFileTools and runs IO
    :return: None
    """
    out, err = node.exec_command(cmd="ls -ll /tmp/")
    output = out.read().decode()
    if not re.search("smallfile", output):
        node.exec_command(
            cmd="cd /tmp/; git clone https://github.com/distributed-system-analysis/smallfile.git",
            sudo=True,
        )
    file_obj = SmallFileTools(node=node, **kwargs)
    file_obj.complete_prereqs()
    file_obj.run_file_write_ops()
    file_obj.run_file_read_ops()


class TestCaseFailureException(Exception):
    """
    Exception is raised when there is a test case failure
    """

    def __init__(self, message="Test case Failed"):
        self.message = f"Test case failed. Error : {message}"
        super().__init__(self.message)


class TestBedSetupFailure(Exception):
    """
    Exception is raised when there is failure in environment set-up for the scenario
    """

    def __init__(self, message="Failed to set-up ENV for test execution"):
        self.message = f"Test bed setup failed. Error : {message}"
        super().__init__(self.message)


class RadosBenchExecutor(RgwIoTools, RadosIoTools, RbdFioTools, SmallFileTools):
    """
    Benchmark Executor with ThreadPoolExecutor
    """

    def __init__(self, ceph_cluster, **kwargs):
        """
        Initialize Benchmark Executor
        Args:
            mon_node: monitor node
            clients: list of clients
        """
        self.SIGStop = False
        self.config = kwargs.get("config")
        self.client_node = ceph_cluster.get_nodes(role="client")[0]
        self.rgw_nodes = ceph_cluster.get_nodes(role="rgw")
        self.active_runs = []

    def stop_signal(self):
        return self.SIGStop

    def initiate_stop_signal(self):
        self.SIGStop = True

    def teardown(self):
        """
        cleanup bench mark residues
        - remove pools
        """
        map(self.initiate_stop_signal(), self.active_runs)

    def run(self):
        """
        Execute benchmark,
           - Create necessary pools
           - Initiate executor
           - Submit thread
           - return
        Args:
            config: benchmark execution config
        config:
            duration: benchmark duration
            pg_num: placement group number
            pool_per_client: boolean
                # True - pool per client
                # False - one pool used by all clients
        """

        config = self.config
        client_node = self.client_node
        rgw_nodes = self.rgw_nodes

        with cf.ProcessPoolExecutor() as exe:
            if config.get("RGW"):
                rgw_config = config["RGW"]
                self.active_runs.append(
                    exe.submit(
                        run_rgw_io, node=client_node, rgw_nodes=rgw_nodes, **rgw_config
                    )
                )
            if config.get("Rados_Bench"):
                rados_config = config["Rados_Bench"]
                self.active_runs.append(
                    exe.submit(run_rados_io, node=client_node, **rados_config)
                )
            if config.get("RBD"):
                rbd_config = config["RBD"]
                self.active_runs.append(
                    exe.submit(run_block_io, node=client_node, **rbd_config)
                )
            if config.get("CephFS"):
                cephfs_config = config["CephFS"]
                self.active_runs.append(
                    exe.submit(run_file_io, node=client_node, **cephfs_config)
                )

        for item in cf.as_completed(self.active_runs):
            log.info(item.result())
