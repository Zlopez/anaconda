#
# Copyright (C) 2019  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
import gi
gi.require_version("BlockDev", "2.0")
from gi.repository import BlockDev as blockdev

from blivet import util as blivet_util, udev, arch
from blivet.errors import StorageError
from blivet.flags import flags as blivet_flags
from blivet.iscsi import iscsi

from pyanaconda.anaconda_logging import program_log_lock
from pyanaconda.core.configuration.anaconda import conf
from pyanaconda.errors import errorHandler as error_handler, ERROR_RAISE
from pyanaconda.modules.common.constants.objects import DISK_SELECTION, AUTO_PARTITIONING, \
    DISK_INITIALIZATION, FCOE, ZFCP
from pyanaconda.modules.common.constants.services import STORAGE
from pyanaconda.storage.osinstall import InstallerStorage
from pyanaconda.platform import platform

from pyanaconda.anaconda_loggers import get_module_logger
log = get_module_logger(__name__)


def enable_installer_mode():
    """Configure Blivet for use by Anaconda."""
    blivet_util.program_log_lock = program_log_lock

    # always enable the debug mode when in the installer mode so that we
    # have more data in the logs for rare cases that are hard to reproduce
    blivet_flags.debug = True

    # We don't want image installs writing backups of the *image* metadata
    # into the *host's* /etc/lvm. This can get real messy on build systems.
    if conf.target.is_image:
        blivet_flags.lvm_metadata_backup = False

    # Set the flags.
    blivet_flags.auto_dev_updates = True
    blivet_flags.selinux_reset_fcon = True
    blivet_flags.keep_empty_ext_partitions = False
    blivet_flags.discard_new = True
    blivet_flags.selinux = conf.security.selinux
    blivet_flags.dmraid = conf.storage.dmraid
    blivet_flags.ibft = conf.storage.ibft
    blivet_flags.multipath_friendly_names = conf.storage.multipath_friendly_names
    blivet_flags.allow_imperfect_devices = conf.storage.allow_imperfect_devices

    # Platform class setup depends on flags, re-initialize it.
    platform.update_from_flags()

    # Load plugins.
    if arch.is_s390():
        load_plugin_s390()

    # Set the blacklist.
    udev.device_name_blacklist = [r'^mtd', r'^mmcblk.+boot', r'^mmcblk.+rpmb', r'^zram', '^ndblk']

    # We need this so all the /dev/disk/* stuff is set up.
    udev.trigger(subsystem="block", action="change")


def create_storage():
    """Create the storage object.

    :return: an instance of the Blivet's storage object
    """
    storage = InstallerStorage()

    # Set the default filesystem type.
    storage.set_default_fstype(conf.storage.file_system_type or storage.default_fstype)

    # Set the default LUKS version.
    storage.set_default_luks_version(conf.storage.luks_version or storage.default_luks_version)

    return storage


def set_storage_defaults_from_kickstart(storage):
    """Set the storage default values from a kickstart file.

    FIXME: A temporary workaround for UI.
    """
    # Set the default filesystem types.
    auto_part_proxy = STORAGE.get_proxy(AUTO_PARTITIONING)
    fstype = auto_part_proxy.FilesystemType

    if auto_part_proxy.Enabled and fstype:
        storage.set_default_fstype(fstype)
        storage.set_default_boot_fstype(fstype)


def load_plugin_s390():
    """Load the s390x plugin."""
    # Don't load the plugin in a dir installation.
    if conf.target.is_directory:
        return

    # Is the plugin loaded? We are done then.
    if "s390" in blockdev.get_available_plugin_names():
        return

    # Otherwise, load the plugin.
    plugin = blockdev.PluginSpec()
    plugin.name = blockdev.Plugin.S390
    plugin.so_name = None
    blockdev.reinit([plugin], reload=False)


def initialize_storage(storage):
    """Perform installer-specific storage initialization.

    :param storage: an instance of the Blivet's storage object
    """
    storage.shutdown()

    while True:
        try:
            reset_storage(storage)
        except StorageError as e:
            if error_handler.cb(e) == ERROR_RAISE:
                raise
            else:
                continue
        else:
            break


def select_all_disks_by_default(storage):
    """Select all disks for the partitioning by default.

    It will select all disks for the partitioning if there are
    no disks selected. Kickstart uses all the disks by default.

    :param storage: an instance of the Blivet's storage object
    :return: a list of selected disks
    """
    disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
    selected_disks = disk_select_proxy.SelectedDisks
    ignored_disks = disk_select_proxy.IgnoredDisks

    if not selected_disks:
        selected_disks = [d.name for d in storage.disks if d.name not in ignored_disks]
        disk_select_proxy.SetSelectedDisks(selected_disks)
        log.debug("Selecting all disks by default: %s", ",".join(selected_disks))

    return selected_disks


def reset_storage(storage):
    """Reset the storage.

    FIXME: A temporary workaround for UI,

    :param storage: an instance of the Blivet's storage object
    """
    # Update the config.
    update_storage_config(storage.config)

    # Set the ignored and exclusive disks.
    disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
    storage.ignored_disks = disk_select_proxy.IgnoredDisks
    storage.exclusive_disks = disk_select_proxy.SelectedDisks

    # Reload additional modules.
    if not conf.target.is_image:
        iscsi.startup()

        fcoe_proxy = STORAGE.get_proxy(FCOE)
        fcoe_proxy.ReloadModule()

        if arch.is_s390():
            zfcp_proxy = STORAGE.get_proxy(ZFCP)
            zfcp_proxy.ReloadModule()

    # Do the reset.
    storage.reset()


def update_storage_config(config):
    """Update the storage configuration.

    :param config: an instance of StorageDiscoveryConfig
    """
    disk_init_proxy = STORAGE.get_proxy(DISK_INITIALIZATION)
    config.clear_part_type = disk_init_proxy.InitializationMode
    config.clear_part_disks = disk_init_proxy.DrivesToClear
    config.clear_part_devices = disk_init_proxy.DevicesToClear
    config.initialize_disks = disk_init_proxy.InitializeLabelsEnabled
    config.zero_mbr = disk_init_proxy.FormatUnrecognizedEnabled
