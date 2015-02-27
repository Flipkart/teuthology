import logging
import os
import subprocess
import tempfile
import yaml

from .config import config
from .contextutil import safe_while
from .misc import decanonicalize_hostname, get_distro, get_distro_version
from .lockstatus import get_status


log = logging.getLogger(__name__)


class Downburst(object):
    def __init__(self, name, os_type, os_version, status=None):
        self.name = name
        self.os_type = os_type
        self.os_version = os_version
        self.status = status or get_status(self.name)
        self.config_path = None
        self.host = decanonicalize_hostname(self.status['vm_host']['name'])

    def create(self):
        self.build_config()
        success = None
        with safe_while(sleep=60, tries=3) as proceed:
            while proceed():
                (returncode, stdout, stderr) = self._run_create()
                if returncode == 0:
                    log.info("Downburst created %s: %s" % (self.name,
                                                           stdout.strip()))
                    success = True
                    break
                elif stderr:
                    # If the guest already exists first destroy then re-create:
                    if 'exists' in stderr:
                        success = False
                        log.info("Guest files exist. Re-creating guest: %s" %
                                 (self.name))
                        self.destroy()
                    else:
                        success = False
                        log.info("Downburst failed on %s: %s" % (
                            self.name, stderr.strip()))
                        break
            return success

    def _run_create(self):
        if not self.config_path:
            raise ValueError("I need a config_path!")
        executable = self.executable
        if not executable:
            log.error("No downburst executable found.")
            return False
        shortname = decanonicalize_hostname(self.name)

        args = [executable, '-c', self.host, 'create',
                '--meta-data=%s' % self.config_path, shortname,
                ]
        log.info("Provisioning a {distro} {distroversion} vps".format(
            distro=self.os_type,
            distroversion=self.os_version
        ))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        out, err = proc.communicate()
        return (proc.returncode, out, err)

    def build_config(self):
        config_fd = tempfile.NamedTemporaryFile(delete=False)

        file_info = {
            'disk-size': '100G',
            'ram': '1.9G',
            'cpus': 1,
            'networks': [
                {'source': 'front', 'mac': self.status['mac_address']}],
            'distro': self.os_type.lower(),
            'distroversion': self.os_version,
            'additional-disks': 3,
            'additional-disks-size': '200G',
            'arch': 'x86_64',
        }
        fqdn = self.name.split('@')[1]
        file_out = {'downburst': file_info, 'local-hostname': fqdn}
        yaml.safe_dump(file_out, config_fd)
        self.config_path = config_fd.name
        return True

    @property
    def executable(self):
        """
        First check for downburst in the user's path.
        Then check in ~/src, ~ubuntu/src, and ~teuthology/src.
        Return '' if no executable downburst is found.
        """
        if config.downburst:
            return config.downburst
        path = os.environ.get('PATH', None)
        if path:
            for p in os.environ.get('PATH', '').split(os.pathsep):
                pth = os.path.join(p, 'downburst')
                if os.access(pth, os.X_OK):
                    return pth
        import pwd
        little_old_me = pwd.getpwuid(os.getuid()).pw_name
        for user in [little_old_me, 'ubuntu', 'teuthology']:
            pth = os.path.expanduser(
                "~%s/src/downburst/virtualenv/bin/downburst" % user)
            if os.access(pth, os.X_OK):
                return pth
        return ''

    @property
    def is_up(self):
        pass

    def destroy(self):
        executable = self.executable
        if not executable:
            log.error("No downburst executable found.")
            return False
        shortname = decanonicalize_hostname(self.name)
        args = [executable, '-c', self.host, 'destroy', shortname]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,)
        out, err = proc.communicate()
        if err:
            log.error("Error destroying {machine}: {msg}".format(
                machine=self.name, msg=err))
            return False
        elif proc.returncode == 0:
            log.info("%s destroyed: %s" % (self.name, out))
            return True
        else:
            log.error("I don't know if the destroy of {node} succeded!".format(
                node=self.name))
            return False

    def remove_config(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
            self.config_path = None
            return True
        return False

    def __del__(self):
        self.remove_config()


def create_if_vm(ctx, machine_name):
    """
    Use downburst to create a virtual machine
    """
    status_info = get_status(machine_name)
    if not status_info.get('is_vm', False):
        return False
    os_type = get_distro(ctx)
    os_version = get_distro_version(ctx)

    has_config = hasattr(ctx, 'config') and ctx.config is not None
    if has_config and 'downburst' in ctx.config:
        log.warning(
            'Usage of a custom downburst config has been deprecated.'
        )

    dbrst = Downburst(machine_name=machine_name, os_type=os_type,
                      os_version=os_version, status=status_info)
    return dbrst.create()


def destroy_if_vm(ctx, machine_name, user=None, description=None):
    """
    Use downburst to destroy a virtual machine

    Return False only on vm downburst failures.
    """
    status_info = get_status(machine_name)
    os_type = get_distro(ctx)
    os_version = get_distro_version(ctx)
    if not status_info or not status_info.get('is_vm', False):
        return True
    if user is not None and user != status_info['locked_by']:
        log.error("Tried to destroy {node} as {as_user} but it is locked by {locked_by}".format(
            node=machine_name, as_user=user, locked_by=status_info['locked_by']))
        return False
    if description is not None and description != status_info['description']:
        log.error("Tried to destroy {node} with description {desc_arg} but it is locked with description {desc_lock}".format(
            node=machine_name, desc_arg=description, desc_lock=status_info['description']))
        return False
    dbrst = Downburst(machine_name=machine_name, os_type=os_type,
                      os_version=os_version, status=status_info)
    return dbrst.destroy()
