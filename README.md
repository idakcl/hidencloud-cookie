# HidenCloud 自动续费

<p align="left">
  <img src="https://img.shields.io/github/stars/SunshineList/hidencloud_renew?style=flat-square&logo=github" alt="GitHub stars">
  <img src="https://img.shields.io/github/forks/SunshineList/hidencloud_renew?style=flat-square&logo=github" alt="GitHub forks">
  <img src="https://img.shields.io/github/actions/workflow/status/SunshineList/hidencloud_renew/hidencloud_renew.yml?style=flat-square&logo=github-actions" alt="GitHub workflow status">
  <img src="https://img.shields.io/github/license/SunshineList/hidencloud_renew?style=flat-square" alt="GitHub license">
</p>

HidenCloud 自动续费脚本，支持多账号、TG 通知、Cookie 自动更新。

## 核心功能
- **自动续期**：默认续期 7 天。
- **自动扣费**：检测到未支付订单时自动用账户余额支付。
- **TG 推送**：包含账号、余额、执行结果及每日一言。
- **持久化**：配合 GitHub PAT 可自动回写 Cookie，不用频繁手动更新 Secret。

---

## 快速配置 (GitHub Actions)

### 1. 获取 Cookie
1. 浏览器登录 [HidenCloud](https://hidencloud.com)。
2. 按 `F12` 打开开发者工具，点击 `Network` (网络) 标签。
3. 刷新页面，找到任意一个请求，在 `Request Headers` (请求头) 中找到 `cookie` 字段。
4. 复制那一长串内容（包含 `hidencloud_session` 等）。

### 2. 设置 Secrets
在 GitHub 仓库的 `Settings` -> `Secrets and variables` -> `Actions` 下添加：

- **`HIDEN_COOKIEa**: 刚才复制的 Cookie。如果要跑多账号，用 `&` 或换行符隔开。
- **`TG_BOT_TOKEN`**: 联系 [@BotFather) 创建机器人获取。
- **`TG_CHAT_ID@**: 给 [@userinfobot](https://t.me/userinfobot) 发消息获取。
- **`GH_PAT`**: (可选) [在此生成](https://github.com/settings/tokens)，勾选 `repo` 权限。用于让脚本自动更新 Cookie。

---

## 本地运行

如果想先在本地跑一下：
1. 安装依赖：
   ```bash
   pip install curl_cffi beautifulsoup4 pynacl
   ```
2. 修改 `config.json` 填入信息。
3. 执行：
   ```bash
   python hidencloud_renew.py
   ```

---

## 常见问题
- **为什么登录失败？**
  脚本目前不走账号密码登录（为了绕过 Cloudflare 验证码），只认 Cookie。如果提示失效，请按照上面的步骤重新抓取。
- **GitHub Actions 没跑？**
  确认 `.github/workflows` 文件夹在项目最根部，不要塞进子文件夹里。
- **Cookie 自动更新不生效？**
  检查 `GH_PAT` 是否配置正确且具备 `repo` 权限。

---

> 🔄 **Keepalive** — 2026-08-20 | 由 **Agnes** (qwenbot) 维护，每 10 天自动心跳保活

> 🔄 **Keepalive** — 2026-08-20 10:22 | 由 **小高** (openclaw) 手动保活

> 🔄 **Keepalive** — 2026-08-20 10:39 | 由 **小高** (openclaw) 手动保活

> 🔄 **Keepalive** — 2026-08-20 02:42 | 由 **元宝** (qwenbot) 手动保活


> ✅ Checked by Agnes at 2026-08-25 07:00:03

> ✅ Checked by Agnes at 2026-08-27 03:37:22

> ✅ Checked by Agnes at 2026-08-28 15:00:39

> ✅ Checked by Agnes at 2026-08-29 15:00:48

> ✅ Checked by Agnes at 2026-08-30 15:00:57