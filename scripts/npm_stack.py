#!/usr/bin/env python3
"""Standalone, root-only Linux Docker/NPM provisioning. No SSH implementation."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_DIR = '/root/data/docker_data/nginx-proxy-manager'
DEFAULT_IMAGE = 'jc21/nginx-proxy-manager:2.15.1'
PACKAGES = ['docker-ce', 'docker-ce-cli', 'containerd.io',
            'docker-buildx-plugin', 'docker-compose-plugin']
MARKER = '.ssh-skill-npm.json'

if os.name == 'posix':
    import fcntl


class Failure(Exception):
    pass


def run(argv, *, timeout=900, check=True):
    # Never stream subprocess output: bootstrap commands may contain secrets in
    # their diagnostics. Only a short, generic error leaves this boundary.
    if argv[0] == 'docker':
        argv = ['docker', '--host', 'unix:///var/run/docker.sock', *argv[1:]]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
    except subprocess.TimeoutExpired:
        raise Failure('Command timed out; outcome may be unknown. Inspect state before retrying.') from None
    if check and result.returncode:
        raise Failure(f'{Path(argv[0]).name} failed (exit {result.returncode}); inspect the service locally.')
    return result


def private_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.npm-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_root(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or len(path.parts) < 4:
        raise Failure('Use an absolute dedicated stack directory, at least three levels deep.')
    if path.resolve() != path or os.path.ismount(path):
        raise Failure('Stack directory must not contain symlinks.')
    return path


def compose_document(args):
    return {'services': {'app': {
        'image': args.image, 'restart': 'unless-stopped',
        'ports': ['80:80', f'{args.admin_bind}:{args.admin_port}:81', '443:443'],
        'volumes': ['./data:/data', './letsencrypt:/etc/letsencrypt'],
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
    }}}


def project_name(root):
    return 'ssh-npm-' + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def compose(root, *args, overlay=None):
    cmd = ['docker', 'compose', '--project-name', project_name(root),
           '--project-directory', str(root), '-f', str(overlay or root / 'compose.yaml')]
    return run(cmd + list(args))


def docker_ready():
    if not shutil.which('docker'):
        raise Failure('Docker is missing. Run docker install --apply first.')
    run(['docker', 'info'], timeout=30)
    run(['docker', 'compose', 'version'], timeout=30)


def load_managed(root):
    marker = root / MARKER
    if not marker.is_file() or marker.is_symlink():
        raise Failure('Directory is not owned by this script; refusing to adopt or remove it.')
    value = json.loads(marker.read_text())
    path = root / 'compose.yaml'
    if value.get('root') != str(root) or value.get('project') != project_name(root):
        raise Failure('Ownership marker does not match this directory.')
    if not path.is_file() or path.is_symlink() or json.loads(path.read_text()) != value['compose']:
        raise Failure('Compose file was changed outside this script; refusing to overwrite or execute it.')
    return value


def credentials(path):
    if not path:
        raise Failure('First installation requires --credentials-file (private JSON: email, password).')
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise Failure('Credentials must be a regular file owned by the current user, mode 600 or 400.')
    data = json.loads(path.read_text())
    if (not isinstance(data.get('email'), str) or '@' not in data['email'] or
            not isinstance(data.get('password'), str) or len(data['password']) < 8):
        raise Failure('Credentials require a valid email and a password of at least 8 characters.')
    return {'email': data['email'], 'password': data['password']}


def port_conflicts(ports):
    busy = []
    # Inspect Docker bindings too: published ports may use NAT without a listener.
    if shutil.which('docker'):
        ids = run(['docker', 'ps', '-q'], timeout=30).stdout.split()
        if ids:
            containers = json.loads(run(['docker', 'inspect', *ids], timeout=30).stdout)
            for container in containers:
                for bindings in (container.get('NetworkSettings', {}).get('Ports') or {}).values():
                    for binding in bindings or []:
                        port = int(binding['HostPort'])
                        if port in ports:
                            busy.append({'port': port, 'owner': container['Name'].lstrip('/')})
    for port in ports:
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            if family == socket.AF_INET6 and not socket.has_ipv6:
                continue
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.bind((address, port))
            except OSError as exc:
                if exc.errno in (97, 99):  # IPv6 disabled on this host
                    continue
                busy.append({'port': port, 'owner': 'host listener or bind failure'})
    return busy


def wait_web(args, login=None):
    host = '127.0.0.1' if args.admin_bind == '0.0.0.0' else args.admin_bind
    url = f'http://{host}:{args.admin_port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url)
            if login:
                body = json.dumps({'identity': login['email'], 'secret': login['password']}).encode()
                request = urllib.request.Request(url + '/api/tokens', data=body,
                                                  headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=3) as response:
                if response.status == 200 and (not login or json.load(response).get('token')):
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise Failure('NPM readiness/login timed out. Data retained; inspect status before retrying.')


def npm_install(args, root):
    docker_ready()
    document = compose_document(args)
    if root.exists() and any(root.iterdir()):
        managed = load_managed(root)
        if managed['compose'] != document:
            raise Failure('Existing installation settings differ. Reuse original options; no implicit upgrade.')
        if managed.get('state') == 'ready':
            compose(root, 'up', '-d')
            wait_web(args)
            return {'state': 'ready', 'changed': False, 'directory': str(root)}
        # Explicit retry of an incomplete managed deployment, preserving its DB.
        credentials(args.credentials_file)
        compose(root, 'down')
    secret = credentials(args.credentials_file)
    conflicts = port_conflicts([80, args.admin_port, 443])
    if conflicts:
        raise Failure('Port conflict: ' + json.dumps(conflicts) + '. Keep 80/443; release the owner or choose another admin port.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Empty directories are accepted; existing data is restored explicitly by the user.
    private_json(root / 'compose.yaml', document)
    managed = {'root': str(root), 'project': project_name(root), 'compose': document, 'state': 'initializing'}
    private_json(root / MARKER, managed)
    overlay = root / '.bootstrap.json'
    try:
        # NPM 2.15.1 logs initial passwords. Disable bootstrap container logging.
        # $$ escapes Compose interpolation while preserving the actual password.
        bootstrap = json.loads(json.dumps(document))
        bootstrap['services']['app']['environment'] = {
            'INITIAL_ADMIN_EMAIL': secret['email'].replace('$', '$$'),
            'INITIAL_ADMIN_PASSWORD': secret['password'].replace('$', '$$'),
        }
        bootstrap['services']['app']['logging'] = {'driver': 'none'}
        private_json(overlay, bootstrap)
        compose(root, 'config', '--quiet', overlay=overlay)
        compose(root, 'pull')
        compose(root, 'up', '-d', overlay=overlay)
        wait_web(args, login=secret)
    finally:
        overlay.unlink(missing_ok=True)
        # Remove bootstrap environment even if initialization/readiness failed.
        # Stop+remove only our service, keeping bind-mounted data and certificates.
        compose(root, 'rm', '--stop', '--force', 'app')
    compose(root, 'up', '-d')
    wait_web(args, login=secret)
    managed['state'] = 'ready'
    private_json(root / MARKER, managed)
    return {'state': 'ready', 'directory': str(root), 'admin_port': args.admin_port,
            'admin_email': secret['email'], 'image': args.image}


def npm_uninstall(args, root):
    if not root.exists():
        return {'state': 'absent'}
    load_managed(root)
    docker_ready()
    compose(root, 'down')
    if args.purge_data:
        # Ownership alone is insufficient: reject linked/mounted subtrees too.
        for parent, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(parent) / name
                if (path.is_symlink() and not path.resolve().is_relative_to(root)) or os.path.ismount(path):
                    raise Failure('Refusing data purge across external symlinks or mount points.')
        shutil.rmtree(root)
        return {'state': 'removed', 'data_preserved': False}
    return {'state': 'stopped', 'data_preserved': True, 'directory': str(root)}


def os_release():
    data = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            data[key] = value.strip('"')
    distro, codename = data.get('ID'), data.get('VERSION_CODENAME', '')
    if distro not in ('ubuntu', 'debian') or not re.fullmatch('[a-z]+', codename):
        raise Failure('Automatic Docker installation/removal supports Debian and Ubuntu only.')
    if not Path('/run/systemd/system').exists():
        raise Failure('A systemd host is required; do not run inside a container.')
    return distro, codename


def docker_install():
    distro, codename = os_release()
    if shutil.which('docker'):
        docker_ready()
        return {'state': 'ready', 'changed': False}
    conflicts = ['docker.io', 'docker-compose', 'docker-compose-v2', 'docker-doc',
                 'podman-docker', 'containerd', 'runc']
    for package in conflicts:
        result = run(['dpkg-query', '-W', '-f=${Status}', package], check=False)
        if 'install ok installed' in result.stdout:
            raise Failure(f'Conflicting package {package}; migrate explicitly before installing Docker CE.')
    run(['apt-get', 'update'])
    run(['apt-get', 'install', '-y', 'ca-certificates', 'curl'])
    key = Path('/etc/apt/keyrings/docker-ssh-skill.asc')
    source = Path('/etc/apt/sources.list.d/docker-ssh-skill.sources')
    arch = run(['dpkg', '--print-architecture']).stdout.strip()
    if arch not in ('amd64', 'arm64'):
        raise Failure('This NPM deployment supports amd64 and arm64 hosts.')
    expected_source = (f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\n'
                       f'Suites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n')
    if source.exists() and (source.is_symlink() or source.read_text() != expected_source):
        raise Failure('Managed Docker APT source was modified; inspect before reinstalling.')
    if key.is_symlink():
        raise Failure('Docker signing key must not be a symlink.')
    for path in Path('/etc/apt/sources.list.d').glob('*'):
        if path != source and path.is_file() and 'download.docker.com' in path.read_text(errors='replace'):
            raise Failure('An existing Docker APT source needs manual reconciliation.')
    key.parent.mkdir(parents=True, exist_ok=True)
    run(['curl', '--fail', '--silent', '--show-error', '--location',
         f'https://download.docker.com/linux/{distro}/gpg', '--output', str(key)])
    key.chmod(0o644)
    source.write_text(expected_source)
    source.chmod(0o644)
    run(['apt-get', 'update'])
    run(['apt-get', 'install', '-y', *PACKAGES])
    run(['systemctl', 'enable', '--now', 'docker'])
    docker_ready()
    return {'state': 'ready', 'changed': True}


def docker_uninstall():
    os_release()
    if not shutil.which('docker'):
        return {'state': 'absent', 'data_preserved': True}
    docker_ready()
    if run(['docker', 'ps', '-aq']).stdout.strip():
        raise Failure('Containers still exist (including stopped containers); remove them explicitly first.')
    installed = []
    for package in PACKAGES:
        if 'install ok installed' in run(['dpkg-query', '-W', '-f=${Status}', package], check=False).stdout:
            installed.append(package)
    if not installed:
        raise Failure('Docker was not installed as Docker CE APT packages; refusing an unknown uninstall.')
    run(['apt-get', 'remove', '-y', *installed])
    return {'state': 'removed', 'data_preserved': True,
            'retained': ['/var/lib/docker', '/var/lib/containerd', 'APT repository configuration']}


def check(args, root):
    available = bool(shutil.which('docker'))
    result = {'platform': platform.system(), 'docker_installed': available,
              'directory': str(root), 'npm_managed': (root / MARKER).is_file()}
    if available:
        result['docker_running'] = run(['docker', 'info'], check=False, timeout=30).returncode == 0
        result['compose_available'] = run(['docker', 'compose', 'version'], check=False, timeout=30).returncode == 0
        if result['docker_running']:
            result['port_usage'] = port_conflicts([80, args.admin_port, 443])
            result['containers'] = run(['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.Status}}\t{{.Ports}}']).stdout.splitlines()
    if result['npm_managed']:
        managed = load_managed(root)
        result['npm_state'] = managed['state']
        result['image'] = managed['compose']['services']['app']['image']
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('component', choices=['docker', 'npm'])
    p.add_argument('action', choices=['check', 'install', 'uninstall'])
    p.add_argument('--apply', action='store_true', help='Execute; otherwise mutations are previews')
    p.add_argument('--directory', default=DEFAULT_DIR)
    p.add_argument('--admin-port', type=int, default=81)
    p.add_argument('--admin-bind', default='0.0.0.0', help='IPv4 address for the admin listener')
    p.add_argument('--image', default=DEFAULT_IMAGE)
    p.add_argument('--credentials-file', help='Private JSON file with email and password; never a command-line password')
    p.add_argument('--purge-data', action='store_true', help='NPM uninstall only: remove data and certificates permanently')
    p.add_argument('--wait', type=int, default=180, help='Readiness timeout in seconds')
    return p


def execute(args):
    root = safe_root(args.directory)
    if not 1 <= args.admin_port <= 65535 or args.admin_port in (80, 443):
        raise Failure('Admin port must be 1..65535 and different from 80/443.')
    if ipaddress.ip_address(args.admin_bind).version != 4:
        raise Failure('Admin bind must be an IPv4 address.')
    if args.wait < 1 or not re.fullmatch(r'jc21/nginx-proxy-manager:[A-Za-z0-9_.-]+', args.image):
        raise Failure('Use a positive timeout and a jc21/nginx-proxy-manager image tag.')
    if args.purge_data and (args.component, args.action) != ('npm', 'uninstall'):
        raise Failure('--purge-data is only valid for npm uninstall.')
    if args.action == 'check':
        return check(args, root)
    if not args.apply:
        return {'preview': True, 'component': args.component, 'action': args.action,
                'directory': str(root), 'image': args.image, 'ports': [80, args.admin_port, 443],
                'data_preserved': not args.purge_data,
                'note': 'No changes made. Use --apply to execute. Docker install is a separate action.'}
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise Failure('Apply requires root on a Linux server.')
    # Serialize all provisioning operations on the host, including Docker removal.
    with open('/run/lock/ssh-skill-npm.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Another provisioning operation is running.') from None
        if args.component == 'docker':
            return docker_install() if args.action == 'install' else docker_uninstall()
        return npm_install(args, root) if args.action == 'install' else npm_uninstall(args, root)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        data = execute(args)
        value = {'success': True, 'operation': f'{args.component}.{args.action}', 'data': data}
    except (Failure, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, Failure) else 'Local file/configuration error; inspect paths and permissions.'
        value = {'success': False, 'operation': f'{args.component}.{args.action}', 'error': message}
    print(json.dumps(value, ensure_ascii=False))
    return 0 if value['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
