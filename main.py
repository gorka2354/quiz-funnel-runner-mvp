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

def classify_screen(page: Page):
    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    paywall_indicators = [
        "secure checkout", "card number", "cvv", "payment method", 
        "billing cycle", "expiration date", "expiry date", "subscription plan"
    ]
    price_indicators = ["/month", "/week", "/year", "/day", "billed monthly", "billed weekly"]
    
    has_paywall_text = any(k in t for k in paywall_indicators)
    has_price = any(k in t for k in price_indicators) and ("$" in t or "€" in t or "£" in t)
    
    if has_paywall_text or has_price:
        return 'paywall'
    
    email_indicators = ["email", "address", "mail", "e-mail"]
    inputs = page.locator("input:visible")
    for i in range(inputs.count()):
        p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
        if any(k in p for k in email_indicators): return 'email'
    
    if ("email" in u or "email" in t) and inputs.count() > 0:
        return 'email'
        
    return 'question'

def perform_action(page: Page, screen_type: str, step_num: int):
    try:
        if screen_type == 'paywall': return "stop"
        if screen_type == 'email':
            # Ищем инпут и заполняем
            el = page.locator("input:visible").first
            el.click(); el.fill(f"testuser{int(time.time())}@gmail.com")
            
            # Ищем и кликаем чекбоксы (согласие с условиями)
            checkboxes = page.locator("input[type='checkbox']:visible, [role='checkbox']:visible")
            for i in range(checkboxes.count()):
                try: checkboxes.nth(i).check(force=True, timeout=500)
                except: checkboxes.nth(i).click(force=True, timeout=500)
            
            page.keyboard.press("Enter")
            time.sleep(2)
            
            # Явно ищем кнопку продолжения
            for text in ['Continue', 'Next', 'Submit', 'Get my plan']:
                btn = page.get_by_text(text, exact=False).first
                if btn.is_visible(timeout=500):
                    btn.tap(force=True, timeout=1000)
                    return "email_filled_and_button_clicked"
            
            return "email_filled_via_enter"
            
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
            if btn.is_visible(timeout=500):
                txt = btn.evaluate("node => (node.innerText || '').toLowerCase()")
                if not any(k in txt for k in ['policy', 'terms']):
                    btn.tap(force=True, timeout=1000); return f"act:tap:{text}"

        btns = page.locator("button:visible, [role='button']:visible")
        if btns.count() > 0:
            btns.last.tap(force=True, timeout=1000); return "act:last_btn_tap"
                
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
            log(f"Nav: {url}"); page.goto(url, wait_until='load', timeout=60000)
            step, history = 1, []
            while step <= 80:
                curr_u = page.url
                is_m = any(k in curr_u for k in ["magic", "analyzing", "loading"])
                time.sleep(15 if is_m else 3)
                curr_h = get_screen_hash(page); curr_id = f"{curr_u}|{curr_h}"
                stuck_count = history.count(curr_id)
                log(f"step:{step} | stuck:{stuck_count} | url:{curr_u[:60]}")
                if stuck_count >= 3: log("Stuck."); break
                history.append(curr_id)
                close_popups(page)
                st = classify_screen(page)
                page.screenshot(path=os.path.join(res_dir, f"{step:02d}_{st}.png"), full_page=True)
                act = perform_action(page, st, stuck_count); log(f"act:{act} | type:{st}")
                if st == 'paywall': break
                step += 1
            browser.close()

if __name__ == '__main__':
    with open('config.json', 'r') as f: config = json.load(f)
    run_funnel("https://coursiv.io/dynamic?prc_id=1069", config, config.get('headless', True))
