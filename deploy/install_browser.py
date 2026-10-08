#!/usr/bin/env python3
"""Install private browser display components on Ubuntu 22.04 amd64, without root."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile

REPO = Path(__file__).resolve().parents[1]
PACKAGES = {
    'tigervnc-standalone-server': ('1.12.0+dfsg-4ubuntu0.22.04.1', '1a232f6a8b584a46dd9e86e0576a659f63d07fdcb0324118165a7fccbaecad01'),
    'tigervnc-common': ('1.12.0+dfsg-4ubuntu0.22.04.1', '83d72061209617e512199dc7ae48ea1bcf603d9968aa1c502e3b2a5373391739'),
    'libxfont2': ('1:2.0.5-1ubuntu0.2', 'b04bcf9cead27bd60fb3a00e571517f79fac80f762166e4d4e3981932d3ed33c'),
    'x11-xkb-utils': ('7.7+5build4', 'ba308c745aaeaeb844cc5e3f61442197b54a7c7dcc3046a2737f7e03003b7e08'),
}
NOVNC = '5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=REPO / '.runtime')
    parser.add_argument('--offline', action='store_true', help='Use already downloaded, verified archives')
    args = parser.parse_args()
    release = Path('/etc/os-release').read_text()
    if platform.machine() != 'x86_64' or 'VERSION_ID="22.04"' not in release:
        parser.error('此脚本锁定 Ubuntu 22.04 amd64；其他系统请单独安装 TigerVNC 和 noVNC。')
    os.umask(0o077)
    tools = args.runtime.resolve() / 'browser-tools'
    downloads = tools / 'downloads'
    downloads.mkdir(parents=True, mode=0o700, exist_ok=True)
    manifest = {}
    for package, (version, expected) in PACKAGES.items():
        archives = list(downloads.glob(package + '_*_amd64.deb'))
        match = next((path for path in archives if digest(path) == expected), None)
        if match is None:
            if args.offline:
                parser.error(f'缺少已验证的 {package} 压缩包。')
            subprocess.run(['apt-get','download',package+'='+version], cwd=downloads, check=True)
            match = next((path for path in downloads.glob(package+'_*_amd64.deb')
                          if digest(path)==expected), None)
        if match is None:
            parser.error(f'{package} 校验失败，停止安装。')
        subprocess.run(['dpkg-deb','-x',str(match),str(tools/'root')],check=True)
        manifest[package] = {'version':version,'sha256':expected}
    archive = downloads / 'novnc-v1.6.0.tar.gz'
    if not archive.exists():
        if args.offline:
            parser.error('缺少 noVNC 压缩包。')
        subprocess.run(['curl','--fail','--location','--silent','--show-error',
            'https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v1.6.0','-o',str(archive)],check=True)
    if digest(archive) != NOVNC:
        parser.error('noVNC 校验失败，停止安装。')
    if not (tools/'novnc/core/rfb.js').exists():
        with tarfile.open(archive) as packed:
            packed.extractall(tools, filter='data')
        shutil.move(str(tools/'noVNC-1.6.0'),str(tools/'novnc'))
    binary = tools / 'root/usr/bin/Xtigervnc'
    blob = binary.read_bytes()
    # The Ubuntu binary hardcodes /usr/bin for xkbcomp. This one constant is
    # changed to empty, so Xorg resolves xkbcomp via our private PATH instead.
    # No executable instructions change. Source: Xorg xkb/ddxLoad.c RunXkbComp.
    if blob.count(b'/usr/bin\0') != 1:
        parser.error('TigerVNC 键盘路径常量不匹配，停止安装。')
    binary.write_bytes(blob.replace(b'/usr/bin\0',b'\0'*9))
    binary.chmod(0o700)
    manifest['Xtigervnc_private_path_patch'] = {'sha256':digest(binary), 'change':'xkbcomp uses private PATH'}
    manifest['noVNC'] = {'version':'1.6.0', 'sha256':NOVNC}
    (tools/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (tools/'manifest.json').chmod(0o600)
    missing = subprocess.run(['ldd',str(binary)],capture_output=True,text=True,
        env={**os.environ,'LD_LIBRARY_PATH':str(tools/'root/usr/lib/x86_64-linux-gnu')}).stdout
    if 'not found' in missing:
        parser.error('TigerVNC 仍有缺失库，请检查 ldd；没有修改系统库。')
    print('私有浏览器组件安装完成；未修改系统软件、服务或 Tailscale 路由。')


if __name__ == '__main__':
    main()
