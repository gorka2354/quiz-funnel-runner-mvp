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
                if el.is_visible(timeout=500): found_text = el; break
            if found_text: break
        
        if not found_text: return False
        container = found_text.locator("xpath=./ancestor::*[self::label or self::div][1]").first
        checkbox = container.locator("input[type='checkbox']").first
        if checkbox.count() > 0:
            if not checkbox.is_checked(): checkbox.click(force=True, timeout=1000)
            return checkbox.is_checked()
        container.click(force=True, timeout=1000)
        return True
    except: return False

def classify_screen(page: Page, log_func):
    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    if any(k in t for k in ["card number", "cvv", "mm/yy", "confirm payment"]) or page.locator("input[name*='card']").count() > 0:
        return 'checkout'

    signals = []
    if any(k in t for k in ["€", "$", "£", "₽"]) or re.search(r'\d+[.,]\d+\s*[€$£₽]', t): signals.append("price")
    if any(k in t for k in ["subscribe", "trial", "billed", "week plan", "month plan"]): signals.append("billing")
    if any(k in t for k in ["get my plan", "checkout", "start trial"]): signals.append("cta")

    if len(signals) >= 2 and not re.search(r'\d+/\d+', t): return 'paywall'
    
    inputs = page.locator("input:visible")
    if ("email" in u or "email" in t) and inputs.count() > 0: return 'email'
    for i in range(inputs.count()):
        p = (inputs.nth(i).get_attribute("placeholder") or "").lower()
        if any(k in p for k in ["email", "mail"]): return 'email'
    
    return 'question'

def find_continue_button(page: Page):
    for text in ['Continue', 'Next', 'Get my plan', 'Start', 'Got it']:
        btn = page.get_by_text(text, exact=False).first
        if btn.is_visible(timeout=500) and btn.evaluate("node => node.tagName !== 'A'"):
            return btn
    btn = page.locator("button:visible, [role='button']:visible").last
    if btn.count() > 0 and btn.is_visible(timeout=500):
        return btn
    return None

def wait_for_transition(page: Page, old_url: str, old_hash: str, timeout=3.0):
    start = time.time()
    while time.time() - start < timeout:
        if page.url != old_url or get_screen_hash(page) != old_hash:
            return True
        time.sleep(0.5)
    return False

def perform_action(page: Page, screen_type: str, log_func, results_dir: str, start_hash: str, start_url: str):
    try:
        if screen_type == 'paywall': return "stopped at paywall"
        if screen_type == 'checkout': return "checkout reached"
        
        if screen_type == 'email':
            ensure_privacy_checkbox_checked(page, log_func)
            email_input = page.locator("input:visible").first
            email_input.fill(f"testuser{int(time.time())}@gmail.com")
            btn = find_continue_button(page)
            if btn: btn.click(force=True, timeout=1000)
            else: page.keyboard.press("Enter")
            log_func("Email submitted. Waiting for transition...")
            wait_for_transition(page, start_url, start_hash, timeout=10.0)
            return "email_submitted"
            
        if screen_type == 'question':
            choice_sel = ["[data-testid*='answer' i]:visible", "[class*='Item' i]:visible", "[class*='Card' i]:visible", "label:visible", "button:visible"]
            choices = None
            for s in choice_sel:
                els = page.locator(s)
                if els.count() > 0: choices = els; break
            
            cont_btn = find_continue_button(page)
            if not choices:
                if cont_btn:
                    cont_btn.tap(force=True, timeout=1000)
                    wait_for_transition(page, start_url, start_hash)
                    return "info_continue_pressed"
                return "no_choices_found"

            # 1. Клик по варианту
            choices.first.tap(force=True, timeout=1000)
            
            # 2. Ждем активации кнопки или авто-перехода
            # Мы НЕ выходим из функции, пока не совершим все действия на этом экране
            start_time = time.time()
            while time.time() - start_time < 3.0:
                if page.url != start_url: break # URL изменился - успех
                
                curr_cont = find_continue_button(page)
                if curr_cont and curr_cont.is_enabled():
                    curr_cont.tap(force=True, timeout=1000)
                    break # Кликнули продолжить - успех
                time.sleep(0.5)

            # 3. Если все еще на той же странице и кнопка disabled - Multiselect
            if page.url == start_url:
                curr_cont = find_continue_button(page)
                if curr_cont and not curr_cont.is_enabled():
                    log_func("Multiselect detected. Selecting more options...")
                    for i in range(1, min(choices.count(), 4)):
                        choices.nth(i).tap(force=True, timeout=500)
                        if curr_cont.is_enabled():
                            curr_cont.tap(force=True, timeout=1000)
                            break
            
            # 4. Финальное ожидание завершения перехода
            wait_for_transition(page, start_url, start_hash)
            return "screen_interaction_completed"
                
    except Exception as e: return f"err:{str(e)}"
    return "none"

def run_funnel(url: str, config: dict, is_headless: bool):
    slug = get_slug(url); res_dir = os.path.join('results', slug); os.makedirs(res_dir, exist_ok=True)
    classified_dir = os.path.join('results', '_classified')
    for cat in ['question', 'info', 'input', 'email', 'paywall', 'other', 'checkout']:
        os.makedirs(os.path.join(classified_dir, cat), exist_ok=True)
    
    summary = {"url": url, "slug": slug, "steps_total": 0, "paywall_reached": False, "last_url": "", "path": res_dir, "error": None}
    
    with open(os.path.join(res_dir, 'log.txt'), 'w', encoding='utf-8') as f:
        def log(m):
            l = f"[{time.strftime('%H:%M:%S')}] {m}\n"; f.write(l); print(l.strip())
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=is_headless, slow_mo=100)
            page = browser.new_context(**p.devices['iPhone 13']).new_page()
            log(f"Navigating to {url}"); page.goto(url, wait_until='load', timeout=60000)
            
            step, history = 1, []
            while step <= 80:
                # Стабилизация перед классификацией
                curr_u = page.url
                if any(k in curr_u for k in ["magic", "analyzing", "loading"]): 
                    time.sleep(12); curr_u = page.url
                
                close_popups(page)
                curr_h = get_screen_hash(page)
                st = classify_screen(page, log)
                
                # Проверка stuck (URL + Hash)
                curr_id = f"{curr_u}|{curr_h}"
                if history.count(curr_id) >= 2: # Уменьшаем лимит для скорости
                    log(f"Stuck at {curr_u}. Stopping."); summary["error"] = "stuck_loop"; break
                history.append(curr_id)
                
                # ОДИН скриншот на один уникальный экран
                screen_name = f"{step:02d}_{st}.png"
                local_path = os.path.join(res_dir, screen_name)
                page.screenshot(path=local_path, full_page=True)
                shutil.copy2(local_path, os.path.join(classified_dir, st, f"{slug}__{screen_name}"))
                
                # ВЫПОЛНЕНИЕ ВСЕХ ДЕЙСТВИЙ НА ЭКРАНЕ
                act = perform_action(page, st, log, res_dir, curr_h, curr_u)
                log(f"step:{step} | type:{st} | action:{act} | url:{page.url[:60]}")
                
                summary["steps_total"] = step; summary["last_url"] = page.url
                if st in ['paywall', 'checkout'] or "stopped" in act or "reached" in act:
                    if st == 'paywall': summary["paywall_reached"] = True
                    break
                
                step += 1
            browser.close()
    with open(os.path.join('results', 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump([summary], f, indent=4)

if __name__ == '__main__':
    with open('config.json', 'r') as f: config = json.load(f)
    run_funnel("https://coursiv.io/dynamic?prc_id=1069", config, config.get('headless', True))
