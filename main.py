import json
import argparse
import os
import time
import re
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

def get_slug(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.replace('www.', '')
    path = parsed.path.strip('/').replace('/', '-')
    if path:
        return f"{domain}-{path}"
    return domain

def close_popups(page: Page, log_func):
    # 1. Агрессивное скрытие оверлеев и cookie-баннеров через JS
    try:
        page.evaluate("""
            const selectors = [
                '#consent_blackbar',
                '#truste-consent-track',
                '#trustarc-banner-container',
                '#onetrust-banner-sdk',
                '.onetrust-pc-dark-filter',
                '[role="banner"]',
                '.tc-track',
                'div[id*="cookie" i]',
                'div[class*="cookie" i]',
                'div[id*="consent" i]',
                'div[class*="consent" i]'
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    el.style.display = 'none';
                    el.style.visibility = 'hidden';
                    el.style.opacity = '0';
                    el.style.pointerEvents = 'none';
                });
            });
            
            // Также пытаемся найти fixed элементы на весь экран внизу, которые перехватывают клики
            document.querySelectorAll('div').forEach(el => {
                const style = window.getComputedStyle(el);
                if ((style.position === 'fixed' || style.position === 'sticky') && 
                    (style.bottom === '0px' || parseInt(style.zIndex, 10) > 1000)) {
                    // Исключаем кнопки навигации и важные CTA
                    const text = el.innerText.toLowerCase();
                    if (!text.includes('continue') && !text.includes('next') && !text.includes('claim') && !text.includes('spin') && !text.includes('submit')) {
                        el.style.pointerEvents = 'none';
                    }
                }
            });
        """)
    except Exception as e:
        log_func(f"Error hiding popups via JS: {e}")

    # 2. Попытка кликнуть по стандартным кнопкам закрытия (если они еще остались)
    selectors = [
        "text='Accept'", "text='Agree'", "text='I agree'", "text='OK'", "text='Got it'", "text='Allow all'", "text='Accept all'", "text='Confirm My Choices'",
        "[aria-label='close']", "[aria-label='dismiss']", "text='×'"
    ]
    for _ in range(3):
        popup_closed = False
        for sel in selectors:
            try:
                element = page.locator(sel).first
                if element.is_visible(timeout=500):
                    element.click(timeout=1000, force=True)
                    log_func(f"Closed popup using selector: {sel}")
                    popup_closed = True
                    time.sleep(0.5)
            except Exception:
                pass
        if not popup_closed:
            break

def classify_screen(page: Page) -> str:
    # Ищем видимый текст на странице
    try:
        visible_text = page.locator("body").inner_text().lower()
    except Exception:
        visible_text = ""
        
    html = page.content().lower()
    
    # 1. Paywall (ищем в видимом тексте)
    paywall_keywords = ["/month", "/week", "continue to payment", "start trial", "get my plan", "secure checkout", "payment method", "special discount", "money-back", "billed monthly", "billed weekly"]
    paywall_score = sum(1 for kw in paywall_keywords if kw in visible_text)
    
    if paywall_score >= 1 or page.locator("text='Continue to payment', text='Start trial', text='Subscribe', text='Get my plan', text='Get plan', text='Buy Now', text='BUY NOW'").count() > 0:
        return 'paywall'

    # 2. Email
    if page.locator("input[type='email']").count() > 0 or page.locator("input[placeholder*='email' i]").count() > 0:
        return 'email'
        
    # 3. Input
    # Исключаем явные радио-кнопки и чекбоксы, которые могут быть расценены как обычные инпуты
    text_number_inputs = page.locator("input:not([type='radio']):not([type='checkbox']):not([type='hidden'])")
    visible_input_count = sum(1 for i in range(text_number_inputs.count()) if text_number_inputs.nth(i).is_visible())
    
    if visible_input_count > 0:
        # Check if there are specific inputs like age, weight, height, name
        input_keywords = ["age", "height", "weight", "name", "cm", "kg", "lbs", "ft", "in"]
        if any(kw in visible_text for kw in input_keywords) or visible_input_count >= 1:
            return 'input'

    # 4. Question (Strict)
    specific_option_selectors = [".option", ".card", "input[type='radio']", "input[type='checkbox']", "label:has(input[type='radio'])", "label:has(input[type='checkbox'])", "div[class*='option' i]", "div[class*='answer' i]", "div[class*='choice' i]"]
    total_specific_options = sum(page.locator(sel).count() for sel in specific_option_selectors)
    total_buttons = page.locator("button, [role='button']").count()
    choice_inputs = page.locator("input[type='radio'], input[type='checkbox']")
    # Считаем радио-кнопки/чекбоксы либо сами инпуты, либо их родительские лейблы
    visible_choices = sum(1 for i in range(choice_inputs.count()) if choice_inputs.nth(i).is_visible()) + sum(1 for i in range(page.locator("label:has(input[type='radio']), label:has(input[type='checkbox'])").count()) if page.locator("label:has(input[type='radio']), label:has(input[type='checkbox'])").nth(i).is_visible())

    if total_specific_options >= 2 or ("?" in visible_text and total_buttons >= 2) or visible_choices >= 2:
         return 'question'

    # 5. Info 
    if page.locator("button:has-text('Next'), button:has-text('Continue'), button:has-text('Start'), button:has-text('Got it'), button:has-text('Let\\'s go'), button:has-text('See my results'), button:has-text('Spin'), button:has-text('Claim')").count() > 0:
        return 'info'
        
    # 6. Question (Fallback)
    if total_buttons >= 2:
        return 'question'

    return 'other'

def perform_action(page: Page, screen_type: str, log_func) -> str:
    try:
        if screen_type == 'paywall':
            return "stopped at paywall"
            
        elif screen_type == 'email':
            email_input = page.locator("input[type='email'], input[inputmode='email'], input[placeholder*='email' i]").first
            if email_input.is_visible(timeout=1000):
                email_input.click(force=True)
                email_input.fill("john@example.com")
                time.sleep(0.5)
                
                # Попробуем кликнуть чекбоксы, если они есть
                try:
                    checkboxes = page.locator("div[data-testid='checkbox'], input[type='checkbox']")
                    for i in range(checkboxes.count()):
                        cb = checkboxes.nth(i)
                        if cb.is_visible(timeout=500):
                            cb.click(force=True)
                            time.sleep(0.5)
                except Exception:
                    pass

                # Ищем кнопку отправки, перебираем все возможные варианты
                selectors = ["button[data-testid='email-submit']", "button[type='submit']", "button:has-text('Continue')", "button:has-text('Next')", "button:has-text('CONTINUE')", "button:has-text('NEXT')"]
                clicked = False
                for sel in selectors:
                    elements = page.locator(sel)
                    for i in range(elements.count()):
                        btn = elements.nth(i)
                        if btn.is_visible(timeout=500) and btn.is_enabled():
                            btn.click(force=True)
                            clicked = True
                            break
                    if clicked: break
                            
                if not clicked:
                    page.keyboard.press("Enter")
                return "filled email and submitted"
                
        elif screen_type == 'input':
            html = page.content().lower()
            inputs = page.locator("input[type='number'], input[type='text'], input:not([type='hidden'])")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                if not inp.is_visible(timeout=500): continue
                
                # Примитивная эвристика для заполнения
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                name_attr = (inp.get_attribute("name") or "").lower()
                id_attr = (inp.get_attribute("id") or "").lower()
                type_attr = (inp.get_attribute("type") or "").lower()
                
                # Пропускаем радио-кнопки и чекбоксы, они для question
                if type_attr in ['radio', 'checkbox']: continue
                
                val = "100"
                if "age" in placeholder or "age" in name_attr or "years" in html:
                    val = "30"
                elif "height" in placeholder or "height" in name_attr or "cm" in html:
                    val = "170"
                elif "weight" in placeholder or "weight" in name_attr or "kg" in html:
                    val = "65"
                elif "name" in placeholder or "name" in name_attr or "name" in id_attr:
                    val = "John"
                
                inp.click(force=True)
                inp.fill(val)
                time.sleep(0.5)
            
            # Нажимаем continue, ищем именно видимую кнопку
            selectors = ["button[data-testid*='button']", "button:has-text('CONTINUE')", "button:has-text('Continue')", "button:has-text('Next')", "button[type='submit']", "button"]
            clicked = False
            for sel in selectors:
                elements = page.locator(sel)
                for i in range(elements.count()):
                    btn = elements.nth(i)
                    if btn.is_visible(timeout=500) and btn.is_enabled():
                        btn.click(force=True)
                        clicked = True
                        break
                if clicked: break
                
            if not clicked:
                page.keyboard.press("Enter")
            return "filled input"

        elif screen_type == 'info':
            continue_texts = ['Next', 'Continue', 'Start', "Let's go", 'Далее', 'Got it', 'I got it', 'See my results', 'Spin', 'Claim offer', 'Claim prize', 'Get my plan', 'Claim']
            for text in continue_texts:
                elements = page.locator(f'button:has-text("{text}"), a:has-text("{text}"), div[role="button"]:has-text("{text}")')
                for i in range(elements.count()):
                    btn = elements.nth(i)
                    if btn.is_visible(timeout=500) and btn.is_enabled():
                        btn_text = btn.inner_text().strip()
                        if 'disclaimer' in btn_text.lower():
                            continue
                        
                        try:
                            btn.click(timeout=1000)
                        except Exception:
                            btn.click(force=True)
                            
                        if text in ['Spin', 'Claim', 'Claim offer', 'Claim prize']:
                            time.sleep(8) # Wait for spin animation or API request
                        return f"pressed {text} (actual: {btn_text})"
            
            # Fallback for info (prioritize buttons over links to avoid clicking footer links)
            for sel in ["button:not([disabled])", "div[role='button']", "a"]:
                btns = page.locator(sel)
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    if btn.is_visible(timeout=500):
                        try:
                            if btn.is_enabled():
                                btn_text = btn.inner_text().strip()
                                if 'disclaimer' in btn_text.lower() or 'terms' in btn_text.lower() or 'privacy' in btn_text.lower() or 'language' in btn_text.lower() or not btn_text:
                                    continue
                                try:
                                    btn.click(timeout=1000)
                                except Exception:
                                    btn.click(force=True)
                                return f"pressed fallback continue button (actual: {btn_text})"
                        except Exception:
                            pass

        elif screen_type == 'question' or screen_type == 'other':
            # Сначала пытаемся нажать "Continue" если это 'other'
            if screen_type == 'other':
                 btn = page.locator("button:has-text('Continue'), button:has-text('Next')").first
                 try:
                     if btn.is_enabled():
                         btn.click(timeout=1000)
                         return "clicked continue on other"
                 except Exception:
                     try:
                         btn.click(force=True, timeout=500)
                         return "clicked continue on other (force)"
                     except Exception:
                         pass

            # Обязательно чекаем все чекбоксы (согласия с правилами и т.д.), которые могут блокировать переход
            try:
                checkboxes = page.locator("input[type='checkbox']")
                for i in range(checkboxes.count()):
                    cb = checkboxes.nth(i)
                    try:
                        # Проверяем, не чекнут ли он уже
                        if not cb.is_checked():
                            try:
                                cb.check(force=True, timeout=500)
                            except Exception:
                                pass
                            
                            # Для надежности всегда дергаем через JS
                            cb.evaluate("""node => { 
                                if (!node.checked) {
                                    node.checked = true;
                                    node.dispatchEvent(new Event('change', { bubbles: true }));
                                    node.click(); 
                                }
                            }""")
                            time.sleep(0.5)
                    except Exception:
                        pass
            except Exception:
                pass

            clicked = False
            
            # СПЕЦИАЛЬНАЯ ОБРАБОТКА РАДИОКНОПОК (Playwright check() работает лучше всего для React)
            try:
                radios = page.locator("input[type='radio']")
                if radios.count() > 0:
                    for i in range(radios.count()):
                        radio = radios.nth(i)
                        try:
                            # force=True обходит невидимость стилизованных радиокнопок
                            radio.check(force=True, timeout=1000)
                            clicked = True
                            time.sleep(0.5)
                            break
                        except Exception:
                            # Fallback via JS native setter
                            try:
                                radio.evaluate("""node => {
                                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'checked')?.set;
                                    if (nativeInputValueSetter) {
                                        nativeInputValueSetter.call(node, true);
                                    } else {
                                        node.checked = true;
                                    }
                                    node.dispatchEvent(new Event('click', { bubbles: true }));
                                    node.dispatchEvent(new Event('change', { bubbles: true }));
                                }""")
                                clicked = True
                                time.sleep(0.5)
                                break
                            except Exception:
                                pass
            except Exception:
                pass

            # Если радиокнопок нет или не кликнулись, ищем другие элементы
            if not clicked:
                # Ищем любые элементы, которые могут быть вариантами, начиная с вложенных кнопок
                selectors = [
                    "label:has(input[type='radio']) button", 
                    "[data-testid*='choice' i] button", 
                    "[data-testid*='option' i] button",
                    "[data-testid*='choice' i]", "[data-testid*='option' i]",
                    "div[class*='option' i] button",
                    "label", "div[class*='option' i]", "div[class*='answer' i]", "div[class*='choice' i]", 
                    "div[class*='card' i]", "button:not([disabled])", "[role='button']:not([disabled])", "li"
                ]

                for selector in selectors:
                    elements = page.locator(selector)
                    count = elements.count()
                    if count > 0:
                        for i in range(count):
                            el = elements.nth(i)
                            
                            # Если это контейнер, проверяем нет ли внутри него кнопки
                            try:
                                if el.evaluate("node => node.tagName.toLowerCase()") != "button":
                                    inner_btn = el.locator("button").first
                                    if inner_btn.is_visible(timeout=100):
                                        el = inner_btn
                            except Exception:
                                pass

                            text = ""
                            try:
                                text = el.inner_text().strip().lower()
                            except Exception:
                                pass
                            
                            # Пропускаем кнопки навигации и элементы шапки (язык)
                            if text not in ['back', 'назад', 'continue', 'next', 'skip'] and 'language' not in text and 'english' not in text:
                                try:
                                    # Пытаемся кликнуть обычным способом - это важно для React
                                    el.click(timeout=1000)
                                    clicked = True
                                except Exception:
                                    try:
                                        el.click(timeout=1000, force=True)
                                        clicked = True
                                    except Exception:
                                        try:
                                            # Реальный клик мышью по координатам
                                            box = el.bounding_box()
                                            if box:
                                                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                                clicked = True
                                            else:
                                                el.evaluate("node => node.click()")
                                                clicked = True
                                        except Exception:
                                            pass
                                    
                                if clicked:
                                    time.sleep(0.5)
                                    break
                        if clicked:
                            break
                    
                if not clicked:
                    # Иначе просто пробуем кликнуть первую попавшуюся активную кнопку
                    btns = page.locator("button:not([disabled]), a, [role='button']")
                    for i in range(btns.count()):
                        btn = btns.nth(i)
                        try:
                            text = btn.inner_text().strip().lower()
                            if text not in ['back', 'назад'] and len(text) > 0 and 'language' not in text and 'english' not in text:
                                try:
                                    btn.click(timeout=1000)
                                except Exception:
                                    try:
                                        btn.click(timeout=1000, force=True)
                                    except Exception:
                                        box = btn.bounding_box()
                                        if box:
                                            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                        else:
                                            btn.evaluate("node => node.click()")
                                clicked = True
                                time.sleep(0.5)
                                break
                        except Exception:
                            pass
                            
            if clicked:
                # ALWAYS attempt to press Continue/Next after a selection, just in case it doesn't auto-advance
                continue_selectors = [
                    "button:has-text('Next step')", "button:has-text('Continue')", "button:has-text('Submit')", 
                    "button:has-text('Next')", "button:has-text('Далее')", "button:has-text('NEXT')", "button:has-text('CONTINUE')"
                ]
                continue_clicked = False
                for c_sel in continue_selectors:
                    c_elements = page.locator(c_sel)
                    for c_i in range(c_elements.count()):
                        c_btn = c_elements.nth(c_i)
                        try:
                            if c_btn.is_visible(timeout=500) and c_btn.is_enabled():
                                try:
                                    c_btn.click(timeout=1000)
                                except Exception:
                                    c_btn.click(force=True, timeout=500)
                                continue_clicked = True
                                break
                        except Exception:
                            pass
                    if continue_clicked:
                        break
                
                if continue_clicked:
                    return "clicked option + continue"
                else:
                    return "clicked option (no continue needed)"
            
    except Exception as e:
        return f"error performing action: {str(e)}"
    
    return "no action taken"

def run_funnel(url: str, config: dict, is_headless: bool):
    slug = get_slug(url)
    results_dir = os.path.join('results', slug)
    os.makedirs(results_dir, exist_ok=True)
    
    log_path = os.path.join(results_dir, 'log.txt')
    
    with open(log_path, 'w', encoding='utf-8') as log_file:
        def log(msg):
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            log_line = f"[{timestamp}] {msg}\n"
            log_file.write(log_line)
            print(log_line.strip())
            
        with sync_playwright() as p:
            iphone = p.devices['iPhone 13']
            # Добавим user-agent и touch из спеки (хотя девайс уже содержит их)
            browser = p.chromium.launch(headless=is_headless, slow_mo=config.get('slow_mo_ms', 0))
            context = browser.new_context(**iphone)
            page = context.new_page()
            
            log(f"Navigating to {url}")
            try:
                page.goto(url, wait_until='load', timeout=60000)
            except PlaywrightTimeoutError:
                log("Navigation timeout, proceeding anyway.")
            
            step = 1
            max_steps = config.get('max_steps', 40)
            previous_url = ""
            stuck_count = 0
            
            while step <= max_steps:
                # На magic/loading страницах ждем дольше
                if "magic-page" in page.url or "analyzing" in page.url or "loading" in page.url:
                    time.sleep(10)
                else:
                    time.sleep(4) # Ждем прогрузки анимаций и DOM
                
                current_url = page.url
                
                # Check stuck based on the full URL since SPAs use parameters for navigation
                if current_url == previous_url:
                    stuck_count += 1
                else:
                    stuck_count = 0
                
                if stuck_count >= 3:
                    log("Stuck on the same screen 3 times. Stopping.")
                    break
                previous_url = current_url

                # 1. Закрываем попапы
                close_popups(page, log)
                
                # 2. Классифицируем экран
                screen_type = classify_screen(page)
                
                # 3. Делаем скриншот и дамп HTML
                step_str = f"{step:02d}"
                screenshot_filename = f"{step_str}_{screen_type}.png"
                html_filename = f"{step_str}_{screen_type}.html"
                screenshot_path = os.path.join(results_dir, screenshot_filename)
                html_path = os.path.join(results_dir, html_filename)
                try:
                    page.screenshot(path=screenshot_path, full_page=True)
                    with open(html_path, 'w', encoding='utf-8') as html_file:
                        html_file.write(page.content())
                except Exception as e:
                    log(f"Failed to take screenshot or dump HTML: {e}")
                
                # 4. Выполняем действие
                if "magic-page" in current_url and screen_type not in ['email', 'input', 'paywall']:
                    action_desc = "waiting on magic/loading page"
                else:
                    action_desc = perform_action(page, screen_type, log)
                
                log(f"step: {step} | url: {current_url} | detected_type: {screen_type} | action_taken: {action_desc} | screenshot: {screenshot_filename}")
                
                if screen_type == 'paywall':
                    log("Paywall reached. Stopping funnel.")
                    break
                
                step += 1

            if step > max_steps:
                log("max_steps reached")
                
            browser.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Quiz Funnel Runner - MVP")
    parser.add_argument('--config', default='config.json', help='Path to config file')
    parser.add_argument('--headless', type=lambda x: (str(x).lower() == 'true'), default=None, help='Run in headless mode')
    args = parser.parse_args()
    
    with open(args.config, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    headless_mode = args.headless if args.headless is not None else config.get('headless', True)
    
    url = config['funnels'][0]
    run_funnel(url, config, headless_mode)