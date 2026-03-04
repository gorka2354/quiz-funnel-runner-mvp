import json, argparse, os, time, re, hashlib, shutil
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

def ensure_privacy_checkbox_checked(page: Page, log_func) -> bool:
    try:
        keywords = ["I have read and understood", "consent to the processing", "personal data"]
        found_text = None
        for kw in keywords:
            elements = page.get_by_text(kw, exact=False)
            for i in range(elements.count()):
                el = elements.nth(i)
                if el.is_visible(timeout=500):
                    found_text = el
                    break
            if found_text: break
        
        if not found_text: return False

        container = found_text.locator("xpath=./ancestor::*[self::label or self::div][1]").first
        checkbox = container.locator("input[type='checkbox']").first
        if checkbox.count() > 0:
            if not checkbox.is_checked(): checkbox.click(force=True, timeout=1000)
            is_checked = checkbox.is_checked()
            log_func(f"privacy_checked={is_checked} | privacy_target=input_checkbox")
            return is_checked

        role_checkbox = container.locator("[role='checkbox']").first
        if role_checkbox.count() > 0:
            if role_checkbox.get_attribute("aria-checked") != "true":
                role_checkbox.click(force=True, timeout=1000)
            is_checked = role_checkbox.get_attribute("aria-checked") == "true"
            log_func(f"privacy_checked={is_checked} | privacy_target=role_checkbox")
            return is_checked

        container.click(force=True, timeout=1000)
        has_class = container.evaluate("node => node.className.toLowerCase().includes('checked') || node.className.toLowerCase().includes('active')")
        log_func(f"privacy_checked={has_class} | privacy_target=container_click")
        return True
    except Exception as e:
        log_func(f"Error in ensure_privacy_checkbox: {e}")
        return False

def classify_screen(page: Page, log_func):
    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    checkout_indicators = ["card number", "cvv", "mm/yy", "confirm payment", "paypal buy now", "secure checkout"]
    checkout_inputs = page.locator("input[name*='card'], input[autocomplete*='cc-']").count()
    has_checkout_text = any(k in t for k in checkout_indicators)
    if has_checkout_text or checkout_inputs > 0:
        log_func(f"checkout_signals: text={has_checkout_text}, inputs={checkout_inputs}")
        return 'checkout'

    progress_pattern = re.search(r'\d+/\d+', t)
    has_progress = progress_pattern is not None
    
    signals = []
    if any(k in t for k in ["€", "$", "£", "₽"]) or re.search(r'\d+[.,]\d+\s*[€$£₽]', t): signals.append("currency_price")
    billing_keywords = ["subscribe", "trial", "billed", "per week", "per month", "week plan", "month plan", "12-week plan", "4-week plan"]
    if any(k in t for k in billing_keywords): signals.append("subscription_billing")
    cta_keywords = ["get my plan", "buy", "checkout", "confirm payment", "start trial", "get plan"]
    if any(k in t for k in cta_keywords): signals.append("cta_payment")

    paywall_score = len(signals)
    if paywall_score >= 2:
        if has_progress and "currency_price" not in signals:
            log_func(f"Paywall signals detected ({signals}), but progress bar '{progress_pattern.group(0)}' found. Treating as question.")
        else:
            log_func(f"paywall_signals: {signals} | score: {paywall_score}")
            return 'paywall'
    
    inputs = page.locator("input:visible")
    if ("email" in u or "email" in t) and inputs.count() > 0: return 'email'
    for i in range(inputs.count()):
        p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
        if "email" in p or "mail" in p: return 'email'
        
    return 'question'

def perform_action(page: Page, screen_type: str, step_num: int, log_func, results_dir: str):
    try:
        if screen_type == 'paywall': return "stopped at paywall"
        if screen_type == 'checkout': return "checkout reached (safety stop)"
        
        if screen_type == 'email':
            for attempt in range(2):
                ensure_privacy_checkbox_checked(page, log_func)
                email_input = page.locator("input:visible").first
                email_input.click()
                email_input.fill(f"testuser{int(time.time())}@gmail.com")
                
                btn = page.get_by_text("Continue", exact=False).first
                if btn.is_visible(timeout=500): btn.click(force=True, timeout=1000)
                else: page.keyboard.press("Enter")
                
                time.sleep(2.5)
                error_msg = page.get_by_text("please accept our Privacy Policy", exact=False)
                if error_msg.is_visible(timeout=500):
                    log_func("Red error detected. Retrying email step...")
                    continue
                return "email_submitted_successfully"
            return "error:email_failed_after_retries"
            
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
                btn.tap(force=True, timeout=1000); return f"act:tap:{text}"

        btns = page.locator("button:visible, [role='button']:visible")
        if btns.count() > 0:
            btns.last.tap(force=True, timeout=1000); return "act:last_btn_tap"
                
    except Exception as e: return f"err:{str(e)}"
    return "none"

def create_classified_folders(base_dir='results'):
    classified_dir = os.path.join(base_dir, '_classified')
    categories = ['question', 'info', 'input', 'email', 'paywall', 'other', 'checkout']
    for cat in categories:
        os.makedirs(os.path.join(classified_dir, cat), exist_ok=True)
    return classified_dir

def run_funnel(url: str, config: dict, is_headless: bool):
    slug = get_slug(url)
    res_dir = os.path.join('results', slug)
    os.makedirs(res_dir, exist_ok=True)
    classified_dir = create_classified_folders('results')
    
    summary = {
        "url": url,
        "slug": slug,
        "steps_total": 0,
        "paywall_reached": False,
        "last_url": "",
        "path": res_dir,
        "error": None
    }
    
    with open(os.path.join(res_dir, 'log.txt'), 'w', encoding='utf-8') as f:
        def log(m):
            l = f"[{time.strftime('%H:%M:%S')}] {m}\n"; f.write(l); print(l.strip())
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=is_headless, slow_mo=100)
            page = browser.new_context(**p.devices['iPhone 13']).new_page()
            log(f"Navigating to {url}")
            
            try:
                page.goto(url, wait_until='load', timeout=60000)
                step, history = 1, []
                while step <= 80:
                    curr_u = page.url
                    is_m = any(k in curr_u for k in ["magic", "analyzing", "loading"])
                    time.sleep(15 if is_m else 3)
                    curr_h = get_screen_hash(page); curr_id = f"{curr_u}|{curr_h}"
                    stuck_count = history.count(curr_id)
                    log(f"step:{step} | stuck:{stuck_count} | url:{curr_u[:60]}")
                    
                    if stuck_count >= 3: 
                        log("Stuck on the same screen. Stopping.")
                        summary["error"] = "stuck_loop"
                        break
                        
                    history.append(curr_id)
                    close_popups(page)
                    st = classify_screen(page, log)
                    
                    # Сохраняем скриншот и копируем в _classified
                    screen_name = f"{step:02d}_{st}.png"
                    local_path = os.path.join(res_dir, screen_name)
                    classified_path = os.path.join(classified_dir, st, f"{slug}__{screen_name}")
                    
                    page.screenshot(path=local_path, full_page=True)
                    shutil.copy2(local_path, classified_path)
                    
                    act = perform_action(page, st, stuck_count, log, res_dir)
                    log(f"action: {act} | type: {st}")
                    
                    summary["steps_total"] = step
                    summary["last_url"] = page.url
                    
                    if st in ['paywall', 'checkout'] or "stopped" in act or "reached" in act:
                        if st == 'paywall': summary["paywall_reached"] = True
                        break
                    if "error" in act:
                        summary["error"] = act
                        break
                        
                    step += 1
            except Exception as e:
                log(f"Fatal error: {e}")
                summary["error"] = str(e)
            finally:
                browser.close()
                
    # Сохраняем summary.json
    summary_path = os.path.join('results', 'summary.json')
    
    # Загружаем существующий summary, если есть
    existing_summaries = []
    if os.path.exists(summary_path):
        try:
            with open(summary_path, 'r', encoding='utf-8') as f:
                existing_summaries = json.load(f)
                if not isinstance(existing_summaries, list): existing_summaries = [existing_summaries]
        except: pass
        
    # Обновляем или добавляем текущий прогон
    updated = False
    for i, s in enumerate(existing_summaries):
        if s.get("slug") == slug:
            existing_summaries[i] = summary
            updated = True
            break
    if not updated:
        existing_summaries.append(summary)
        
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(existing_summaries, f, indent=4, ensure_ascii=False)

if __name__ == '__main__':
    with open('config.json', 'r') as f: config = json.load(f)
    run_funnel("https://coursiv.io/dynamic?prc_id=1069", config, config.get('headless', True))
