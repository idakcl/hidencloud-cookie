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
    try:
        return "Just a moment" in driver.title or "/cdn-cgi/challenge-platform" in driver.page_source
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


# ============ 单个服务续期 ============
def renew_one(driver, sid):
    """返回 (ok: bool, msg: str)。以「到期时间是否变晚」为准。"""
    manage_url = f"{BASE_URL}/service/{sid}/manage"
    driver.get(manage_url)
    time.sleep(3)
    if not wait_pass_cf(driver):
        return False, "CF 挑战未通过"

    before_raw, before_std = get_due_date(driver)

    # 定位 Renew 按钮
    btn = None
    for by, val in [
        ("css selector", "button[onclick*='showRenewAlert']"),
        ("xpath", "//button[contains(text(),'Renew')]"),
    ]:
        try:
            cand = driver.find_element(by, val)
            if cand.is_displayed():
                btn = cand
                break
        except Exception:
            continue
    if not btn:
        return False, "未找到 Renew 按钮"

    # 解析限制参数（若未到期，站点会弹窗限制）
    onclick = btn.get_attribute("onclick") or ""
    m = re.search(r"showRenewAlert\((\d+),\s*(\d+),\s*(true|false)\)", onclick)
    if m:
        days_left, threshold = int(m.group(1)), int(m.group(2))
        if days_left > threshold:
            return True, f"未到期 (剩余 {days_left} 天, 需≤{threshold})"

    btn.click()
    time.sleep(2)

    # 限制弹窗
    try:
        h3 = driver.execute_script("var e=document.querySelector('.fixed.inset-0 h3');return e?e.textContent.trim():'';")
        if "Renewal Restricted" in (h3 or ""):
            try:
                driver.find_element("xpath", "//button[contains(text(),'OK')]").click()
            except Exception:
                pass
            return True, "未到期 (站点限制, 视为正常)"
    except Exception:
        pass

    # 续期模态框: 真实浏览器会自行完成模态内的 Turnstile
    modal = f"div#renewService-{sid}"
    try:
        driver.wait_for_element_visible(modal, timeout=15)
    except Exception:
        return False, "续期模态框未出现"

    # 尝试触发模态内 Turnstile 并等待 token（拿不到也继续，交给浏览器/最终比对判定）
    if driver.is_element_present(f"{modal} .cf-turnstile"):
        wait_renew_token(driver, sid, timeout=45)

    try:
        driver.find_element(by="css selector", value=f"{modal} button[type='submit']").click()
    except Exception as e:
        return False, f"点击 Create Invoice 失败: {e}"
    time.sleep(4)
    if not wait_pass_cf(driver):
        return False, "创建账单后 CF 挑战未通过"

    # 支付：若有 Pay 按钮则点击（免费服务通常无）
    try:
        clicked = driver.execute_script(
            "var b=document.querySelector('button[type=submit]');"
            "if(b&&b.innerText.toLowerCase().includes('pay')){b.click();return true;}return false;"
        )
        if clicked:
            time.sleep(5)
            wait_pass_cf(driver, timeout=40)
    except Exception:
        pass

    # 复核到期时间
    driver.get(manage_url)
    time.sleep(3)
    wait_pass_cf(driver, timeout=40)
    after_raw, after_std = get_due_date(driver)

    if before_std and after_std:
        if after_std > before_std:
            return True, f"续期成功 ({before_raw} → {after_raw})"
        return False, f"到期时间未变 ({after_raw})"
    if after_std and not before_std:
        return True, f"续期成功 (到期 {after_raw})"
    return False, "无法确认到期时间"


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
    cookie = os.environ.get("HIDEN_COOKIE", "").strip()
    creds = os.environ.get("HIDENCLOUD", "").strip()  # 可选: email-----password
    if not cookie and not creds:
        logger.error("未提供 HIDEN_COOKIE 或 HIDENCLOUD")
        send_tg("❌ HidenCloud 续期失败: 缺少 HIDEN_COOKIE / HIDENCLOUD")
        sys.exit(1)

    driver = Driver(headless=True, uc=True, no_sandbox=True, disable_gpu=True,
                    user_data_dir=PROFILE_DIR, window_size="1280,900")
    driver.set_page_load_timeout(60)
    ok_all = True
    lines = []

    try:
        if cookie:
            inject_cookies(driver, cookie)
        driver.get(f"{BASE_URL}/dashboard")
        time.sleep(3)
        if not wait_pass_cf(driver):
            raise RuntimeError("CF 挑战持续未通过 (数据中心 IP 可能被拦)")

        # 若落到登录页且有账号密码，则走密码登录
        if "/auth/login" in driver.current_url or driver.is_element_visible("input#password"):
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

        for sid in sids:
            ok, msg = renew_one(driver, sid)
            lines.append(f"{'✅' if ok else '❌'} 服务 {sid}: {msg}")
            if not ok:
                ok_all = False

    except Exception as e:
        ok_all = False
        lines.append(f"❌ 异常: {e}")
        try:
            os.makedirs("screenshots", exist_ok=True)
            driver.save_screenshot("screenshots/fail.png")
        except Exception:
            pass
    finally:
        # 导出最新 cookie 写回 Secret，保持登录态新鲜
        new_cookie = export_cookie_str(driver)
        if new_cookie and new_cookie != cookie:
            update_github_secret(new_cookie)
        driver.quit()

    report = "📊 *HidenCloud 浏览器续期*\n\n" + "\n".join(lines)
    photo = "screenshots/fail.png" if not ok_all and os.path.exists("screenshots/fail.png") else None
    send_tg(report, photo=photo)
    logger.info("结果:\n" + report)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
