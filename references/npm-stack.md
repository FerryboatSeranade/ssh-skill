# Docker 与 Nginx Proxy Manager 部署

用于给明确的 SSH 服务器检查、安装、卸载 Docker 和 NPM。服务器上运行
`scripts/npm_stack.py`（独立 Python 3.10+ 脚本，只依赖标准库）。
SSH 上传及远程执行仍通过 `scripts/ssh_skill.py`，不要直接使用 SSH/SCP。

## 支持范围与默认值

- Docker 自动安装/卸载：使用 systemd 的 Debian/Ubuntu，Docker 官方 APT 源。
  遇到发行版 Docker、Podman 或现有 containerd 等冲突包，停止并报告，不能直接替换。
- NPM：已有可用 Docker Engine + Compose v2 的 Linux 服务器，root 执行。
- 固定 HTTP/HTTPS 端口 80/443；管理端口默认 81，可用 `--admin-port 8181`。
- 默认目录 `/root/data/docker_data/nginx-proxy-manager`，默认镜像
  `jc21/nginx-proxy-manager:2.15.1`；可显式传 `--image`，含 `latest`。
- SQLite 数据与证书分别在 `data/`、`letsencrypt/`。不用过时的 Compose `version`。
- 管理端口默认绑定 `0.0.0.0`，可以指定 `--admin-bind 127.0.0.1` 配合 SSH 隧道。
  Docker 发布端口可能绕过 UFW，应按用户要求配置云安全组/访问范围。
- 检查及预览没有修改；安装/卸载必须加 `--apply`。NPM 安装不会隐式安装 Docker。

## 快速调用流程

先明确目标别名，按本 skill 的平台说明解析 `<SSH_SKILL_ROOT>`。
远程脚本统一放在 root 私有目录 `/root/.local/lib/ssh-skill/npm_stack.py`。
以下为操作步骤，不是一次性拼接执行的本地 shell：每一步解析结果后再继续。

1. 用 `ssh_skill.py exec ALIAS "install -d -m 700 /root/.local/lib/ssh-skill"`
   创建目录，再用 `ssh_skill.py upload ALIAS LOCAL_SCRIPT REMOTE_SCRIPT` 上传脚本。
2. 用 `ssh_skill.py exec ALIAS "python3 /root/.local/lib/ssh-skill/npm_stack.py docker check"`
   检查环境。非 root 用户需先确认具备免交互 sudo，再以相应身份执行和安排私有目录。
3. Docker 缺失时先执行 `docker install` 预览，再按已授权范围运行
   `docker install --apply`。外层 SSH `--timeout 1200`。
4. 首次 NPM 安装需要 JSON 凭据文件：字段 `email` 和 `password`。
   本机默认私有配置可放在 `~/.config/ssh-skill/npm-admin.json`，权限 600；
   不存在时让用户提供私有文件或在终端隐式输入，不把密码放在命令行或仓库。
   将其上传到已创建的 root 私有目录下 `npm-admin.json`，然后执行
   `chmod 600 /root/.local/lib/ssh-skill/npm-admin.json`。不要 cat 文件。
5. 用下面的远程命令预览，随后加 `--apply` 安装；SSH 超时用 1200 秒：

   ```text
   python3 /root/.local/lib/ssh-skill/npm_stack.py npm install --credentials-file /root/.local/lib/ssh-skill/npm-admin.json
   ```

6. 成功或失败后都通过 `ssh_skill.py exec` 删除上传的凭据文件。
   若 SSH 结果未知，先只读确认运行状态，不能自动重放安装。
7. 外层 SSH JSON 的命令 stdout 是脚本的内层 JSON，两层都要检查成功与否。
   报告目录、镜像和管理入口，不展示密码、token、容器完整 inspect 或初始化日志。

## 初始化与重复运行

首次使用临时私有 Compose 配置传入初始管理员邮箱/密码。固定版本的 NPM 会在
初始化日志打印密码，所以临时容器使用 `logging.driver=none`。
本地 HTTP API 登录验证成功后移除临时文件和容器，以不含凭据的正式 Compose
重新启动，并再次验证登录。失败也会尝试删除临时容器和配置，保留数据。

重复相同安装参数会确保容器启动并等待 Web 可用，不重置账户、不拉取新镜像。
更改端口/镜像参数或手改 Compose 后会拒绝覆盖；版本升级与迁移应单独安排。
未完成的受管安装可以在人工确认状态后，用原凭据和原参数显式重试；不会重置
已建立的管理员。恢复已有数据库时凭据必须与原账户一致。

默认超时 `--wait 180` 秒可调整。超时不等于回滚；读取检查结果后决定下一步。
端口占用报告 Docker 容器名或主机监听冲突；需要时再用远程 `ss -ltnp` 定位。
不要自动停止不属于此脚本的服务来释放 80/443。

## 卸载

```text
python3 npm_stack.py npm uninstall
python3 npm_stack.py npm uninstall --apply
python3 npm_stack.py npm uninstall --purge-data --apply
python3 npm_stack.py docker uninstall
python3 npm_stack.py docker uninstall --apply
```

NPM 默认只执行受管项目的 Compose down，保留 Compose、数据库、证书和镜像，
再次运行原安装命令即可恢复。`--purge-data` 永久删除受管目录，只在用户明确
要求清除数据时使用；先确认备份。未带所有权标记的目录及修改过的 Compose
不会被接管或删除，含指向目录外的符号链接或挂载点时拒绝清除；证书目录内部的符号链接可正常清除。

Docker 卸载拒绝存在任何容器（包括已停止容器）的主机；仅移除 Docker CE
软件包，不删除 `/var/lib/docker`、`/var/lib/containerd`、APT 源或用户项目数据。

## 开发与同步上游

在 `custom` 分支开发、测试、提交，推送到 `origin/custom`。
获取原作者更新：先提交本地修改，再 `git fetch upstream`、`git merge upstream/main`，
解决冲突并运行 `python3 -m unittest discover -s tests -v` 后推送。
本机全局入口链接当前 checkout，未提交更改也会影响实际调用。

## 依据

- [Docker Ubuntu 安装/卸载](https://docs.docker.com/engine/install/ubuntu/)
- [Docker Debian 安装/卸载](https://docs.docker.com/engine/install/debian/)
- [NPM 官方 Compose 配置](https://nginxproxymanager.com/setup/)
- [NPM 2.15.1 管理员初始化源码](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.15.1/backend/setup.js)
