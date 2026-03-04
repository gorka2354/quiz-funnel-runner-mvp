import json, argparse, os, time, re, hashlib
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError

def get_slug(url: str):
    p = urlparse(url); d = p.netloc.replace('www.', ''); pth = p.path.strip('/').replace('/', '-')
    return f"{d}-{pth}" if pth else d

def get_screen_hash(page: Page):
    try:
        t = page.evaluate("() => (document.body.innerText || '').slice(0, 5000)")
        return hashlib.md5(t.encode('utf-8')).hexdigest()
    except: return ""

def close_popups(page: Page):
    try:
        page.evaluate("""
            const sel = ['#onetrust-banner-sdk', '.onetrust-pc-dark-filter', '#consent_blackbar'];
            sel.forEach(s => { const el = document.querySelector(s); if(el) el.remove(); });
        """)
    except: pass

def ensure_privacy_consent(page: Page, log_func, results_dir: str) -> bool:
    try:
        log_info = {"found": False, "target": "none", "checked": False}
        
        # 1. Поиск текста Privacy (строго исключаем клики по <a>)
        keywords = ["Privacy Policy", "Terms of Service", "I agree", "Privacy"]
        found_text = None
        for kw in keywords:
            elements = page.get_by_text(kw, exact=False)
            for i in range(elements.count()):
                el = elements.nth(i)
                if el.is_visible(timeout=500) and el.evaluate("node => node.tagName !== 'A' && node.closest('a') === null"):
                    found_text = el
                    break
            if found_text: break
        
        if found_text:
            log_info["found"] = True
            # Ищем ближайший кликабельный контейнер (лейбл или див), который НЕ ссылка
            container = found_text.locator("xpath=./ancestor::*[self::label or self::div or self::p][1]").first
            
            # Проверяем наличие чекбокса внутри
            checkbox = container.locator("input[type='checkbox'], [role='checkbox']").first
            if checkbox.count() > 0:
                checkbox.click(force=True, timeout=1000)
                log_info["target"] = "inner_checkbox"
                log_info["checked"] = True
            else:
                # Кликаем по самому контейнеру через JS для надежности
                container.evaluate("node => node.click()")
                log_info["target"] = "container_js_click"
                log_info["checked"] = True
        else:
            # Fallback: любой видимый чекбокс
            cb = page.locator("input[type='checkbox']:visible, [role='checkbox']:visible").first
            if cb.count() > 0:
                cb.click(force=True, timeout=1000)
                log_info["found"] = True
                log_info["target"] = "fallback_checkbox"
                log_info["checked"] = True

        log_func(f"consent_checkbox_found={log_info['found']} | consent_click_target={log_info['target']} | consent_checked_after={log_info['checked']}")
        return log_info["checked"]
    except Exception as e:
        log_func(f"Error in ensure_privacy_consent: {e}")
        return False

def classify_screen(page: Page):
    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    paywall_indicators = ["secure checkout", "card number", "cvv", "payment method", "billing cycle", "subscription plan", "payment summary", "total to pay"]
    price_indicators = ["/month", "/week", "/year", "billed monthly"]
    if any(k in t for k in paywall_indicators) or (any(k in t for k in price_indicators) and ("$" in t or "€" in t)):
        return 'paywall'
    
    # Email detection (более надежный)
    inputs = page.locator("input:visible")
    if "email" in u or "email" in t or "address" in t:
        if inputs.count() > 0: return 'email'
    for i in range(inputs.count()):
        p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
        if any(k in p for k in ["email", "mail"]): return 'email'
        
    return 'question'

def perform_action(page: Page, screen_type: str, step_num: int, log_func, results_dir: str):
    try:
        if screen_type == 'paywall': return "stop"
        
        if screen_type == 'email':
            # 1. Privacy Consent
            ensure_privacy_consent(page, log_func, results_dir)
            # 2. Fill Email
            email_input = page.locator("input:visible").first
            email_input.click()
            email_input.fill(f"testuser{int(time.time())}@gmail.com")
            # 3. Submit (через клик по кнопке с текстом)
            submit_clicked = "enter"
            for text in ['Continue', 'Next', 'Submit', 'Get my plan']:
                btn = page.get_by_text(text, exact=False).first
                # Кликаем только если это не ссылка
                if btn.is_visible(timeout=500) and btn.evaluate("node => node.tagName !== 'A'"):
                    btn.click(force=True, timeout=1000)
                    submit_clicked = text
                    break
            else:
                page.keyboard.press("Enter")
            return f"email_filled=true | submit_clicked={submit_clicked}"
            
        # Standard question handling
        choice_sel = ["[data-testid*='answer' i]:visible", "[class*='Item' i]:visible", "[class*='Card' i]:visible", "label:visible"]
        clicked_any = False
        for s in choice_sel:
            els = page.locator(s)
            if els.count() > 0:
                limit = 8 if step_num > 0 else 1
                for i in range(min(els.count(), limit)):
                    target = els.nth(i); target.scroll_into_view_if_needed()
                    target.tap(force=True, timeout=1000)
                    target.evaluate("node => setTimeout(() => node.click(), 100)")
                    clicked_any = True
                break
        
        if clicked_any: time.sleep(2)

        for text in ['Continue', 'Next', 'Get my plan', 'Start', 'Got it']:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible(timeout=500) and btn.evaluate("node => node.tagName !== 'A'"):
                btn.click(force=True, timeout=1000); return f"act:click:{text}"

        btns = page.locator("button:visible, [role='button']:visible")
        if btns.count() > 0:
            btns.last.click(force=True, timeout=1000); return "act:last_btn_click"
                
    except Exception as e: return f"err:{str(e)}"
    return "none"

def run_funnel(url: str, config: dict, is_headless: bool):
    slug = get_slug(url); res_dir = os.path.join('results', slug); os.makedirs(res_dir, exist_ok=True)
    with open(os.path.join(res_dir, 'log.txt'), 'w', encoding='utf-8') as f:
        def log(m):
            l = f"[{time.strftime('%H:%M:%S')}] {m}\n"; f.write(l); print(l.strip())
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=is_headless, slow_mo=100)
            page = browser.new_context(**p.devices['iPhone 13']).new_page()
            log(f"Navigating to {url}")
            page.goto(url, wait_until='load', timeout=60000)
            step, history = 1, []
            while step <= 80:
                curr_u = page.url
                is_m = any(k in curr_u for k in ["magic", "analyzing", "loading"])
                time.sleep(15 if is_m else 3)
                curr_h = get_screen_hash(page); curr_id = f"{curr_u}|{curr_h}"
                stuck_count = history.count(curr_id)
                log(f"step:{step} | stuck:{stuck_count} | url:{curr_u[:60]}")
                if stuck_count >= 3: log("Stuck on the same screen. Stopping."); break
                history.append(curr_id)
                close_popups(page)
                st = classify_screen(page)
                page.screenshot(path=os.path.join(res_dir, f"{step:02d}_{st}.png"), full_page=True)
                act = perform_action(page, st, stuck_count, log, res_dir)
                log(f"action: {act} | type: {st}")
                if st == 'paywall': break
                step += 1
            browser.close()

if __name__ == '__main__':
    with open('config.json', 'r') as f: config = json.load(f)
    run_funnel("https://coursiv.io/dynamic?prc_id=1069", config, config.get('headless', True))
