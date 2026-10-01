#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HidenCloud 浏览器版自动续期 (方案A)

- 注入现有 HIDEN_COOKIE 到真实 Chrome (SeleniumBase uc 模式)，
  让浏览器自己完成 Cloudflare/Turnstile，再点站点真实按钮续期。
- 只有真正比较到「到期时间变晚」才记成功，杜绝假绿。
- 失败使进程非 0 退出，GitHub Actions 会显示红色。
- 运行后导出会话最新 cookie 写回 GitHub Secret，保持登录态新鲜。
"""
import os
import re
import sys
import time
import base64
import json
import logging

import requests
from seleniumbase import Driver

try:
    from nacl import encoding, public
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("hiden-browser")

BASE_URL = "https://dash.hidencloud.com"
PROFILE_DIR = os.path.abspath("browser_state")
SHOT_DIR = "screenshots"

STATUS_ICON = {"ok": "✅", "skip": "ℹ️", "fail": "❌"}
STATUS_TEXT = {"ok": "续期成功", "skip": "未到续期时间", "fail": "续期失败"}


def md(s):
    """Telegram Markdown 旧版会吞掉 _ * ` [ 等字符，账号/日期需转义"""
    return re.sub(r"([_*\[\]`])", r"\\\1", s or "")


def bj_time():
    """北京时间字符串 (UTC+8)，不依赖运行环境时区"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))


def take_shot(driver, name):
    """截图并返回路径，失败返回 None"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, f"{time.strftime('%H%M%S')}-{name}.png")
        driver.save_screenshot(path)
        logger.info(f"📸 {path}")
        return path
    except Exception as e:
        logger.warning(f"截图失败 ({name}): {e}")
        return None


# ============ GitHub Secret 自动刷新 ============
def update_github_secret(new_cookie):
    gh_pat = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_pat or not repo:
        logger.warning("未找到 GH_PAT / GITHUB_REPOSITORY，跳过 Secret 更新")
        return
    if not HAS_NACL:
        logger.error("未安装 pynacl，无法加密更新 Secret")
        return
    headers = {"Authorization": f"token {gh_pat}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=headers, timeout=20)
        if r.status_code != 200:
            logger.error(f"获取公钥失败: {r.text}")
            return
        pk = r.json()
        box = public.SealedBox(public.PublicKey(pk["key"].encode("utf-8"), encoding.Base64Encoder()))
        enc = base64.b64encode(box.encrypt(new_cookie.encode("utf-8"))).decode("utf-8")
        r2 = requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/HIDEN_COOKIE",
            headers=headers,
            json={"encrypted_value": enc, "key_id": pk["key_id"]},
            timeout=20,
        )
        logger.info("GitHub Secret HIDEN_COOKIE 已更新" if r2.status_code in (201, 204) else f"更新失败: {r2.text}")
    except Exception as e:
        logger.error(f"更新 Secret 出错: {e}")


# ============ WebDAV (InfiniCloud) 持久化 ============
DAV_FILE = "hiden_cookie.json"
DAV_TIMEOUT = 30


def dav_config():
    """未配置则返回 None，整个云端层自动禁用（fail-open）。"""
    url = (os.environ.get("WEBDAV_URL") or "").strip()
    user = (os.environ.get("WEBDAV_USER") or "").strip()
    pwd = (os.environ.get("WEBDAV_PASS") or "").strip()
    if not url or not user or not pwd:
        return None
    if not url.endswith("/"):
        url += "/"
    return url, user, pwd


def dav_download():
    """取云端最新 cookie，返回 (cookie, 说明)；任何异常/未配置都返回 ("", 原因)。"""
    cfg = dav_config()
    if not cfg:
        logger.info("未配置 WEBDAV_URL/USER/PASS，跳过云端读取")
        return "", "未配置"
    full, user, pwd = cfg[0] + DAV_FILE, cfg[1], cfg[2]
    try:
        r = requests.get(full, auth=(user, pwd), timeout=DAV_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            ck = (data.get("cookie") or "").strip()
            if ck:
                upd = data.get("updated_at", "未知")
                logger.info(f"☁️ 云端 cookie 已获取 (更新于 {upd})")
                return ck, f"更新于 {upd}"
            logger.warning("☁️ 云端文件无 cookie 字段")
            return "", "云端文件无 cookie"
        elif r.status_code == 404:
            logger.info("☁️ 云端暂无 cookie 文件（首次运行）")
            return "", "云端无文件"
        else:
            logger.warning(f"☁️ 云端下载失败，状态码 {r.status_code}")
            return "", f"云端 HTTP {r.status_code}"
    except Exception as e:
        logger.warning(f"☁️ 云端下载异常: {e}")
        return "", "云端读取异常"


def dav_upload(new_cookie):
    if not new_cookie:
        return
    cfg = dav_config()
    if not cfg:
        return
    full, user, pwd = cfg[0] + DAV_FILE, cfg[1], cfg[2]
    body = json.dumps({"cookie": new_cookie, "updated_at": bj_time(),
                       "source": "hidencloud-cookie"}, ensure_ascii=False)
    try:
        r = requests.put(full, data=body.encode("utf-8"), auth=(user, pwd),
                         headers={"Content-Type": "application/json"}, timeout=DAV_TIMEOUT)
        logger.info(f"☁️ 云端 cookie 已更新 ({r.status_code})" if r.status_code in (200, 201, 204)
                    else f"☁️ 云端上传失败: {r.status_code}")
    except Exception as e:
        logger.warning(f"☁️ 云端上传异常: {e}")


# ============ Telegram 通知 ============
def send_tg(message, photo=None):
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        return
    try:
        if photo and os.path.exists(photo):
            with open(photo, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    files={"photo": f},
                    data={"chat_id": chat, "caption": message, "parse_mode": "Markdown"},
                    timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": message, "parse_mode": "Markdown"},
                timeout=15,
            )
        logger.info("TG 通知已发送")
    except Exception as e:
        logger.error(f"TG 发送失败: {e}")


# ============ Cookie 解析/注入/导出 ============
def parse_cookie_str(cookie_str):
    out = []
    for item in (cookie_str or "").split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            if k:
                out.append((k, v))
    return out


def inject_cookies(driver, cookie_str):
    # 必须先建立同域 origin 才能 add_cookie
    try:
        driver.get(f"{BASE_URL}/robots.txt")
    except Exception:
        pass
    n = 0
    for k, v in parse_cookie_str(cookie_str):
        ok = False
        for dom in ("dash.hidencloud.com", ".hidencloud.com"):
            try:
                driver.add_cookie({"name": k, "value": v, "domain": dom, "path": "/"})
                ok = True
                break
            except Exception:
                continue
        if ok:
            n += 1
        else:
            logger.warning(f"注入 cookie {k} 失败")
    logger.info(f"已注入 {n} 个 cookie")


def export_cookie_str(driver):
    pairs = []
    try:
        for c in driver.get_cookies():
            pairs.append(f"{c['name']}={c['value']}")
    except Exception as e:
        logger.error(f"导出 cookie 失败: {e}")
    return "; ".join(pairs)


# ============ Cloudflare 挑战处理 ============
def is_cf_challenge(driver):
    """只用 title / URL 判定，避免把内嵌 CF 脚本的正常页面误判为挑战页。"""
    try:
        t = (driver.title or "")
        u = driver.current_url or ""
        if "Just a moment" in t or "Attention Required" in t:
            return True
        if "Security Verification" in t and "/dashboard" not in u and "/service" not in u:
            return True
        return False
    except Exception:
        return False


def wait_pass_cf(driver, timeout=90):
    """等待 uc 浏览器自动通过 CF 挑战，必要时调用 handle_cf。"""
    start = time.time()
    handled = False
    while time.time() - start < timeout:
        if not is_cf_challenge(driver):
            return True
        if not handled:
            for fn in ("uc_gui_handle_cf", "uc_gui_click_cf", "uc_gui_handle_captcha"):
                try:
                    getattr(driver, fn)()
                    handled = True
                    logger.info(f"尝试通过 CF 挑战: {fn}")
                    break
                except Exception:
                    continue
        time.sleep(2)
    return not is_cf_challenge(driver)


# ============ 到期日期解析 ============
def parse_due_date(text):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", text)
    if m:
        try:
            return time.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y")
        except Exception:
            pass
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return time.strptime(text[:10], "%Y-%m-%d")
        except Exception:
            pass
    return None


def get_due_date(driver):
    try:
        raw = driver.find_element("xpath", "//h6[contains(text(),'Due date')]/following-sibling::div").text.strip()
    except Exception:
        raw = ""
    return raw, parse_due_date(raw)


# ============ 服务列表 ============
def get_service_ids(driver):
    ids = re.findall(r"/service/(\d+)/manage", driver.page_source)
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ============ 账号信息（用户名/邮箱/余额） ============
def get_account_info(driver):
    try:
        return driver.execute_script("""
            function txt(sel){var e=document.querySelector(sel);return e?e.textContent.replace(/\\s+/g,' ').trim():'';}
            var info = {name:'', email:'', balance:''};
            info.email = txt('p.font-light.text-gray-500');
            if (!/@/.test(info.email)) {
                var m = document.body.innerText.match(/[\\w.+-]+@[\\w-]+\\.[\\w.]+/);
                info.email = m ? m[0] : '';
            }
            // 用户名取邮箱所在卡片内的标题，避免抓到页面其它 h3
            var ep = document.querySelector('p.font-light.text-gray-500');
            if (ep) {
                var card = ep.closest('div') || ep.parentElement;
                for (var i = 0; i < 4 && card; i++) {
                    var h = card.querySelector('h3, h4, .font-bold, .text-lg');
                    if (h && h.textContent.trim() && h !== ep) { info.name = h.textContent.replace(/\\s+/g,' ').trim(); break; }
                    card = card.parentElement;
                }
            }
            var link = document.querySelector('h3 > a[href="#"]');
            if (!info.name && link) info.name = link.textContent.replace(/\\s+/g,' ').trim();
            var bl = document.querySelector('a[href*="/balance"]');
            if (bl) {
                var b = bl.querySelector('.font-extrabold, .text-3xl, h4, dt, div');
                var mm = (b ? b.textContent : '').match(/[¥€$]\\s?\\d+(?:[.,]\\d{1,2})?/);
                info.balance = mm ? mm[0].replace(/\\s+/g, '') : '';
            }
            if (!info.balance) {
                var bm = document.body.innerText.match(/[¥€$]\\s?\\d+\\.\\d{2}/);
                info.balance = bm ? bm[0].replace(/\\s+/g, '') : '';
            }
            return info;
        """) or {}
    except Exception as e:
        logger.warning(f"读取账号信息失败: {e}")
        return {}


# ============ 汇总报告 ============
def build_report(acct, counts, lines, cookie_src="", login_via=None):
    total = counts.get("ok", 0) + counts.get("skip", 0) + counts.get("fail", 0)
    try:
        attempt = int(os.environ.get("RENEW_ATTEMPT") or 0) + 1
    except ValueError:
        attempt = 1
    head = "☁️ *HidenCloud 自动续费任务*\n"
    head += "━━━━━━━━━━━━━━━━━━\n"
    who = md(acct.get("name") or "未知")
    if acct.get("email"):
        who += f" ({md(acct['email'])})"
    head += f"👤 账号: {who}\n"
    head += f"💰 余额: {md(acct.get('balance') or '未知')}\n"
    head += f"🕒 时间: {bj_time()} (UTC+8)\n"
    if cookie_src:
        head += f"🍪 Cookie 来源: {md(cookie_src)}\n"
    if login_via:
        head += f"🔑 实际登录: {md(login_via)}\n"
    if attempt > 1:
        head += f"🔁 第 {attempt - 1}/10 次重试\n"
    head += "━━━━━━━━━━━━━━━━━━\n"
    head += (f"📊 执行统计: 成功 {counts.get('ok',0)} | "
             f"未到 {counts.get('skip',0)} | 失败 {counts.get('fail',0)} | 共 {total}\n\n")
    body = "\n".join(lines) if lines else "（无服务）"
    return f"{head}{body}\n━━━━━━━━━━━━━━━━━━\n🤖 GitHub Action/hidencloud-cookie"


# ============ 页面结构 dump（排查用，只含名称/文本，不含 token） ============
def dump_renew_ui(driver, sid):
    try:
        info = driver.execute_script("""
            var sid = arguments[0];
            function norm(t){return (t||'').replace(/\\s+/g,' ').trim();}
            function attrs(el){return {tag:el.tagName.toLowerCase(), id:el.id||'', type:el.getAttribute('type')||'', oc:(el.getAttribute('onclick')||'').slice(0,60), text:norm(el.textContent).slice(0,40)};}
            var btns = Array.from(document.querySelectorAll('button, a[href], input[type=submit]')).map(attrs).filter(function(b){return b.text||b.oc||b.id;});
            return {
              url: location.href,
              has_renew_form: !!document.querySelector('#renew-form-'+sid),
              has_renew_modal: !!document.querySelector('#renewService-'+sid),
              has_turnstile: !!document.querySelector('.cf-turnstile'),
              forms: Array.from(document.querySelectorAll('form')).map(function(f){return {id:f.id||'', action:(f.getAttribute('action')||'').slice(0,60)};}).slice(0,10),
              buttons: btns.slice(0,25)
            };
        """, sid)
        logger.info("UI-DUMP " + str(info))
        return info
    except Exception as e:
        logger.info(f"UI-DUMP-ERR {e}")
        return None


# ============ 单个服务续期 ============
def renew_one(driver, sid):
    """返回 (status, msg, shot)。status: ok=续期成功 / skip=未到续期时间 / fail=失败。

    以「到期时间是否变晚」为唯一硬判据，杜绝假绿。
    """
    manage_url = f"{BASE_URL}/service/{sid}/manage"
    driver.get(manage_url)
    time.sleep(3)
    if not wait_pass_cf(driver):
        return "fail", "CF 挑战未通过", take_shot(driver, f"{sid}-cf-blocked")

    before_raw, before_std = get_due_date(driver)

    # 用 JS 稳健定位并点击 Renew 触发按钮（onclick showRenewAlert 或文本含 renew）
    clicked = driver.execute_script("""
        var sid = arguments[0];
        function norm(t){return (t||'').replace(/\\s+/g,' ').trim();}
        var els = Array.from(document.querySelectorAll('button, a[href], input[type=submit]'));
        // 优先带 showRenewAlert 的
        var el = els.find(function(e){return (e.getAttribute('onclick')||'').indexOf('showRenewAlert')>=0;});
        if (!el) el = els.find(function(e){return /renew/i.test(norm(e.textContent)) || /renew/i.test(norm(e.value));});
        if (el) { el.click(); return norm(el.textContent||el.value).slice(0,40); }
        return null;
    """, sid)

    if not clicked:
        dump_renew_ui(driver, sid)
        return "fail", "未找到 Renew 按钮 (已 dump 结构)", take_shot(driver, f"{sid}-no-renew-btn")
    logger.info(f"点击续期触发按钮: {clicked!r}")
    time.sleep(2)

    # 站点限制弹窗 = 未到续期时间
    restriction = driver.execute_script("""
        var e=document.querySelector('.fixed.inset-0 h3');return e?e.textContent.trim():'';
    """)
    if "Renewal Restricted" in (restriction or ""):
        detail = driver.execute_script("""
            var e=document.querySelector('.fixed.inset-0 p');return e?e.textContent.replace(/\\s+/g,' ').trim():'';
        """) or ""
        try:
            driver.execute_script("var b=Array.from(document.querySelectorAll('button')).find(function(x){return /ok|close/i.test(x.textContent);});if(b)b.click();")
        except Exception:
            pass
        # 从站点提示中提取剩余天数，展示更直观
        dm = re.search(r"expires in\s+(\d+)\s+day", detail, re.I)
        if dm:
            msg = f"还剩 {dm.group(1)} 天到期，需 <1 天才可续"
        else:
            msg = f"站点提示: {detail[:120]}" if detail else "站点提示 Renewal Restricted"
        return "skip", msg, take_shot(driver, f"{sid}-restricted")

    # 续期容器：模态框 #renewService-{sid} 或内联表单 #renew-form-{sid}
    container = None
    for sel in (f"div#renewService-{sid}", f"form#renew-form-{sid}", f"#renew-form-{sid}"):
        try:
            if driver.is_element_present(sel):
                container = sel
                break
        except Exception:
            continue
    if not container:
        try:
            driver.wait_for_element_visible(f"#renewService-{sid}", timeout=10)
            container = f"#renewService-{sid}"
        except Exception:
            dump_renew_ui(driver, sid)
            return "fail", "点击后未出现续期表单/模态框 (已 dump)", take_shot(driver, f"{sid}-no-modal")

    # 容器内若有 Turnstile，尝试交互并等待 token（拿不到也继续，交给浏览器/最终比对判定）
    if driver.is_element_present(f"{container} .cf-turnstile") or driver.is_element_present(".cf-turnstile"):
        wait_renew_token(driver, sid, timeout=45)

    submitted = driver.execute_script("""
        var sel = arguments[0];
        var root = document.querySelector(sel) || document;
        var b = root.querySelector("button[type='submit'], input[type='submit']") ||
                Array.from(root.querySelectorAll('button')).find(function(x){return /renew|invoice|submit|confirm/i.test(x.textContent);});
        if (b) { b.click(); return (b.textContent||b.value||'submit').replace(/\\s+/g,' ').trim().slice(0,30); }
        return null;
    """, container)
    if not submitted:
        dump_renew_ui(driver, sid)
        return "fail", "容器内未找到提交按钮 (已 dump)", take_shot(driver, f"{sid}-no-submit")
    logger.info(f"提交续期表单: {submitted!r}")
    time.sleep(4)
    if not wait_pass_cf(driver):
        return "fail", "创建账单后 CF 挑战未通过", take_shot(driver, f"{sid}-cf-after-submit")

    # 支付：若有 Pay 按钮则点击（免费服务通常无）
    try:
        paid = driver.execute_script(
            "var b=document.querySelector('button[type=submit]');"
            "if(b&&b.innerText.toLowerCase().includes('pay')){b.click();return true;}return false;"
        )
        if paid:
            logger.info("点击 Pay 完成支付")
            time.sleep(5)
            wait_pass_cf(driver, timeout=40)
    except Exception:
        pass

    # 复核到期时间（唯一硬判据）
    driver.get(manage_url)
    time.sleep(3)
    wait_pass_cf(driver, timeout=40)
    after_raw, after_std = get_due_date(driver)
    shot = take_shot(driver, f"{sid}-result")

    if before_std and after_std:
        if after_std > before_std:
            return "ok", f"到期 {before_raw} → {after_raw}", shot
        return "fail", f"到期时间未变 (仍为 {after_raw})", shot
    if after_std and not before_std:
        return "ok", f"到期 {after_raw}", shot
    return "fail", "无法确认到期时间", shot


def wait_renew_token(driver, sid, timeout=60):
    """等待续期表单的 cf-turnstile-response 出 token；期间尝试交互点击。"""
    start = time.time()
    clicked = False
    js = ("var el=document.querySelector('input[name=cf-turnstile-response]');"
          "return el?el.value:'';")
    while time.time() - start < timeout:
        tok = driver.execute_script(js)
        if tok and len(tok) > 20:
            return tok
        if not clicked and driver.is_element_present(".cf-turnstile"):
            for fn in ("uc_gui_click_cf", "uc_gui_click_captcha"):
                try:
                    getattr(driver, fn)()
                    clicked = True
                    break
                except Exception:
                    continue
        time.sleep(1)
    return None


# ============ 主流程 ============
def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    secret_cookie = os.environ.get("HIDEN_COOKIE", "").strip()
    creds = os.environ.get("HIDENCLOUD", "").strip()  # 可选: email-----password
    # 云端优先：WebDAV 里是上次运行刷新的最新 cookie，Secret 只作种子/兜底
    cloud_cookie, cloud_note = dav_download()
    if cloud_cookie:
        cookie, cookie_src = cloud_cookie, f"☁️ InfiniCloud 云端 ({cloud_note})"
    elif secret_cookie:
        cookie, cookie_src = secret_cookie, f"🔒 GitHub Secret HIDEN_COOKIE (云端: {cloud_note})"
    else:
        cookie, cookie_src = "", f"无 (云端: {cloud_note} / Secret 为空)"
    if not cookie and not creds:
        logger.error("未提供 HIDEN_COOKIE 或 HIDENCLOUD")
        send_tg("❌ HidenCloud 续期失败: 缺少 HIDEN_COOKIE / HIDENCLOUD")
        sys.exit(1)

    driver = Driver(headless=True, uc=True, no_sandbox=True, disable_gpu=True,
                    user_data_dir=PROFILE_DIR, window_size="1280,900")
    driver.set_page_load_timeout(60)
    ok_all = True
    lines = []
    shots = []
    counts = {"ok": 0, "skip": 0, "fail": 0}
    acct = {}
    login_via = "Cookie 直接登录" if cookie else "无 Cookie"

    try:
        if cookie:
            inject_cookies(driver, cookie)
        driver.get(f"{BASE_URL}/dashboard")
        time.sleep(3)
        if not wait_pass_cf(driver):
            raise RuntimeError("CF 挑战持续未通过 (数据中心 IP 可能被拦)")

        # 若落到登录页且有账号密码，则走密码登录
        if "/auth/login" in driver.current_url or driver.is_element_visible("input#password"):
            login_via = "账号密码登录 (Cookie 失效)"
            if not creds or "-----" not in creds:
                raise RuntimeError("Cookie 已失效且未配置 HIDENCLOUD 账号密码")
            email, pwd = creds.split("-----", 1)
            logger.info("Cookie 失效，改用账号密码登录")
            if is_cf_challenge(driver):
                wait_pass_cf(driver)
            driver.type("input#username", email)
            driver.type("input#password", pwd)
            time.sleep(3)
            if driver.is_element_present(".cf-turnstile"):
                try:
                    driver.uc_gui_click_cf()
                except Exception:
                    driver.click(".cf-turnstile")
                if not wait_renew_token(driver, "", timeout=60):
                    raise RuntimeError("登录 Turnstile 未通过")
            driver.click("button[type='submit']")
            if not wait_pass_cf(driver, timeout=30):
                raise RuntimeError("登录后仍被 CF 拦截")
            t = time.time()
            while "/dashboard" not in driver.current_url and time.time() - t < 45:
                time.sleep(1)
            if "/auth/login" in driver.current_url:
                raise RuntimeError("账号密码登录失败")

        sids = get_service_ids(driver)
        if not sids:
            raise RuntimeError("登录后未找到任何服务")
        logger.info(f"发现服务: {sids}")
        acct = get_account_info(driver)

        for sid in sids:
            status, msg, shot = renew_one(driver, sid)
            if shot:
                shots.append(shot)
            lines.append(f"{STATUS_ICON[status]} 服务 {sid} · {STATUS_TEXT[status]}\n   └ {md(msg)}")
            counts[status] = counts.get(status, 0) + 1
            if status == "fail":
                ok_all = False

    except Exception as e:
        ok_all = False
        counts["fail"] = counts.get("fail", 0) + 1
        lines.append(f"❌ 异常\n   └ {md(str(e))}")
        s = take_shot(driver, "critical-error")
        if s:
            shots.append(s)
    finally:
        # 导出最新 cookie 双写：GitHub Secret + WebDAV，任一失败不影响另一个
        new_cookie = export_cookie_str(driver)
        if new_cookie and (new_cookie != cookie or new_cookie != secret_cookie):
            update_github_secret(new_cookie)
            dav_upload(new_cookie)
        driver.quit()

    report = build_report(acct, counts, lines, cookie_src, login_via)
    send_tg(report, photo=(shots[-1] if shots else None))
    logger.info("结果:\n" + report)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
