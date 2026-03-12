import json, argparse, os, time, re, hashlib, shutil, random
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page, TimeoutError
from concurrent.futures import ThreadPoolExecutor

REALISTIC_NAMES = ["john.doe", "jane.smith", "alex.wilson", "emma.brown", "mike.taylor", "sarah.jones", "david.clark", "lisa.white"]
REALISTIC_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "icloud.com"]

COOKIE_BLACKLIST = [
    "settings", "preferences", "customize", "options", "more info", 
    "einstellungen", "optionen", "mehr informationen", 
    "cookie settings", "privacy settings", "datenschutzeinstellungen",
    "manage", "preferences"
]

SHARE_BLACKLIST = [
    "share", "sharing", "tell a friend", "invite", "recommend",
    "поделиться", "поделись", "рассказать", "пригласить",
    "view more", "show less", "read more", "back", "zurück", "назад"
]

def is_forbidden_button(el, log_func=None) -> bool:
    try:
        # Check if element is inside a header or footer
        is_nav_area = el.evaluate("""el => {
            const navElements = el.closest('header, footer, nav, [class*="header"], [class*="footer"], [class*="nav"], [class*="menu"], [id*="header"], [id*="footer"], [id*="nav"], [id*="menu"]');
            return navElements !== null;
        }""")
        
        if is_nav_area:
            return True
            
        txt = (el.evaluate("el => el.innerText", timeout=500) or "").lower()
        alabel = (el.get_attribute("aria-label") or "").lower()
        cls = (el.get_attribute("class") or "").lower()
        html = (el.evaluate("el => el.innerHTML", timeout=500) or "").lower()

        full_text = f"{txt} {alabel} {cls} {html}"

        forbidden = False
        reason = ""
        if any(word in full_text for word in COOKIE_BLACKLIST): 
            forbidden = True; reason = "cookie"

        # Use regex with word boundaries for ALL forbidden words to avoid false positives
        forbidden_words = [
            "share", "sharing", "tell a friend", "invite", "recommend",
            "поделиться", "поделись", "рассказать", "пригласить",
            "view more", "show less", "read more", "back", "zurück", "назад",
            "my account", "login", "log in", "signin", "sign in", "contact",
            "faq", "terms", "privacy", "policy", "cookie", "help", "language", "change language",
            "facebook", "instagram", "twitter", "youtube", "pinterest", "menu", "close", "legal"
        ]
        pattern = r'\b(' + '|'.join(re.escape(w) for w in forbidden_words) + r')\b'

        if re.search(pattern, full_text):
            forbidden = True; reason = "forbidden_ui"

        if forbidden and log_func and txt.strip():
            log_func(f"forbidden_skipped={reason} | text: {txt.strip()[:30]}")
        return forbidden
    except: return False

def get_choices_text(page: Page, log_func=None):
    try:
        choice_sel = [
            "[data-testid*='answer' i]:visible", 
            "button:visible", 
            "[class*='Item' i]:visible", 
            "[class*='Card' i]:visible", 
            "label:visible"
        ]
        for s in choice_sel:
            els = page.locator(s)
            texts = []
            for i in range(els.count()):
                curr = els.nth(i)
                if is_forbidden_button(curr, log_func): continue
                try: txt = (curr.evaluate("el => el.innerText", timeout=1000) or "").strip()
                except: txt = ""
                if txt and not re.search(r'\d+\s*/\s*\d+', txt) and len(txt) < 100:
                    texts.append(txt)
            if texts:
                return "|".join(texts[:3])
        return ""
    except: return ""

def get_ui_step(page: Page):
    try:
        # Improved regex: require total >= 10 to avoid 24/7, or ensure current <= total
        def extract_step(text):
            # Look for patterns like "3 / 23" or "3/23"
            matches = re.finditer(r'(\d+)\s*/\s*(\d+)', text)
            for m in matches:
                curr, total = int(m.group(1)), int(m.group(2))
                # Heuristic: quiz progress usually has total > 5 and curr <= total
                # and we specifically ignore 24/7
                if total > 5 and curr <= total and f"{curr}/{total}" != "24/7":
                    return f"{curr}/{total}"
            return None

        # Look in visible text first
        t = page.evaluate("() => document.body.innerText")
        res = extract_step(t)
        if res: return res
        
        # Look in all elements if not found
        res = page.evaluate(r"""() => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            let node;
            while(node = walker.nextNode()) {
                const m = node.textContent.match(/(\d+)\s*\/\s*(\d+)/);
                if (m) {
                    const c = parseInt(m[1]), t = parseInt(m[2]);
                    if (t > 5 && c <= t && m[0].trim() !== "24/7") return m[0].trim();
                }
            }
            return null;
        }""")
        if res: return res.replace(" ", "")
    except: pass
    return "unknown"

def get_slug(url: str):
    p = urlparse(url); d = p.netloc.replace('www.', ''); pth = p.path.strip('/').replace('/', '-')
    slug = f"{d}-{pth}" if pth else d
    if p.query:
        q_hash = hashlib.md5(p.query.encode()).hexdigest()[:6]
        slug = f"{slug}-{q_hash}"
    return slug

def get_screen_hash(page: Page):
    try:
        t = page.evaluate("() => (document.body.innerText || '').slice(0, 10000)")
        return hashlib.md5(t.encode('utf-8')).hexdigest()
    except: return ""

def close_popups(page: Page, log_func):
    try:
        # Fast JS-based popup dismissal
        clicked = page.evaluate("""() => {
            const keywords = ['accept', 'agree', 'allow'];
            const btns = Array.from(document.querySelectorAll('button, [role="button"], a.button'));
            for (const btn of btns) {
                const style = window.getComputedStyle(btn);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                
                const txt = (btn.innerText || '').toLowerCase();
                if (!txt) continue;
                
                // Skip navigation/continue buttons to avoid false positives
                const forbidden = ['continue', 'next', 'submit', 'get my plan', 'start', 'claim', 'spin', 'skip'];
                if (forbidden.some(fw => txt.includes(fw))) continue;
                
                if (keywords.some(kw => txt.includes(kw)) && txt.length < 30) {
                    btn.click();
                    return txt;
                }
            }
            return null;
        }""")
        if clicked:
            log_func(f"cookie_action=accepted_text_fastJS")
            return True
            
        cookie_found = False
        
        # Hidden fallback for giant fixed overlays
        hidden_fallback = page.evaluate("""() => {
            let hidden = false;
            const els = Array.from(document.querySelectorAll('*')).filter(el => {
                const style = window.getComputedStyle(el);
                return (style.position === 'fixed' || style.position === 'sticky') && style.display !== 'none';
            });
            
            els.forEach(el => {
                const t = (el.innerText || '').toLowerCase();
                if (t.includes('cookie') || t.includes('consent') || t.includes('privacy')) {
                    const primary = ['continue', 'next', 'submit', 'get my plan', 'claim', 'spin', 'start'];
                    if (!primary.some(w => t.includes(w))) { 
                        el.style.display = 'none'; 
                        hidden = true;
                    }
                }
            });
            return hidden;
        }""")
        if hidden_fallback:
            log_func("cookie_action=hidden_fallback_css")
            return True
            
        return False
    except: pass


def ensure_privacy_checkbox_checked(page: Page, log_func) -> bool:
    try:
        keywords = ["I have read and understood", "consent to the processing", "personal data", "Terms of service", "By continuing", "I agree", "Terms & Conditions", "Terms and Conditions", "Terms"]
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
            if not checkbox.is_checked():
                try:
                    page.evaluate("(el) => el.click()", checkbox.element_handle())
                except:
                    try:
                        checkbox.click(force=True, timeout=1000)
                    except:
                        container.click(force=True, timeout=1000)
            return checkbox.is_checked()
            
        container.click(force=True, timeout=1000)
        return True
    except: return False

DEBUG_CLASSIFY = True

def classify_screen(page: Page, log_func=None):
    def debug_return(ctype, reason):
        if log_func:
            log_func(f"classify_reason={reason}")
        return ctype

    t = ""
    try: t = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
    except: pass
    u = page.url.lower()
    
    # Check for inputs explicitly
    input_state = None
    try:
        input_state = page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll("input:not([type='hidden']):not([type='checkbox']):not([type='radio']), textarea")).filter(el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.opacity !== '0';
            });
            if (inputs.length === 0) return null;
            
            let is_email = false;
            let is_name = false;
            let is_age = false;
            
            for (let el of inputs) {
                if (el.type === 'email' || (el.autocomplete && el.autocomplete.toLowerCase().includes('email'))) {
                    is_email = true;
                }
                let p = (el.placeholder || "").toLowerCase();
                let n = (el.name || "").toLowerCase();
                if (p.includes('email') || p.includes('mail@') || n.includes('email')) is_email = true;
                if (p.includes('name') || n.includes('name')) is_name = true;
                if (p.includes('age') || p.includes('years') || n.includes('age')) is_age = true;
                
                if (el.id) {
                    let label = document.querySelector(`label[for="${el.id}"]`);
                    if (label) {
                        let lt = label.innerText.toLowerCase();
                        if (lt.includes('email')) is_email = true;
                        if (lt.includes('name')) is_name = true;
                    }
                }
            }
            return { count: inputs.length, is_email, is_name, is_age };
        }""")
    except: pass

    has_visible_inputs = input_state is not None and input_state.get('count', 0) > 0

    heading_text = ""
    try:
        heading_text = page.evaluate("() => Array.from(document.querySelectorAll('h1, h2, .heading')).map(el => el.innerText).join(' ').toLowerCase()")
    except: pass

    # 0. Loading / Transitional Screens
    if not has_visible_inputs:
        loading_kws = ["analyzing", "loading", "magic", "preparing", "generating", "calculating", "personalizing", "processing", "please wait"]
        is_loading_url = any(k in u for k in ["magic-page", "prepareplan", "loading", "analyzing"])
        has_nav = page.locator("button:visible, [role='button']:visible, a.button:visible").filter(has_text=re.compile(r'continue|next|got it|start|proceed', re.I)).count() > 0
        has_choices = page.locator("[data-testid*='answer' i]:visible, [class*='Item' i]:visible, [class*='Card' i]:not([class*='testimonial' i]):not([class*='review' i]):visible, button:visible").count() > 0
        
        if is_loading_url and not has_nav:
            return debug_return('loading', "Transitional/Loading screen detected via URL")
            
        if not has_nav and not has_choices:
            if any(k in u for k in loading_kws) or any(k in t[:500] for k in loading_kws) or any(k in heading_text for k in loading_kws):
                return debug_return('loading', "Transitional/Loading screen detected (no nav buttons or choices)")

    # 1. Checkout
    checkout_kws = ["card number", "cvv", "mm/yy", "confirm payment", "paypal", "buy now", "apple pay", "google pay", "pay now", "order now"]
    if any(k in t for k in checkout_kws) or page.locator("input[name*='card']").count() > 0 or any(k in u for k in ['checkout', 'payment', 'billing']):
        return debug_return('checkout', "Checkout keywords, card inputs, or URL found")

    # 2. Paywall
    has_price = any(k in t for k in ["€", "$", "£", "₽", "usd", "eur", "gbp", "руб", "price", "cost", "total"]) or re.search(r'\d+[.,]\d+\s*[€$£₽]', t)
    has_billing = any(k in t for k in ["subscribe", "trial", "billed", "pricing", "order", "save", "off", "guarantee", "secure", "money-back", "7-day", "billing"])
    has_cta = any(k in t for k in ["get my", "get your", "checkout", "start trial", "claim", "buy now", "pay now", "proceed to my plan", "get my plan"])
    
    signals = [bool(has_price), bool(has_billing), bool(has_cta)]
    if ("coursiv.io" in u and "selling-page" in u) or (sum(signals) >= 2 and has_price) or (sum(signals) >= 3) or any(k in u for k in ['paywall', 'offer', 'selling']):
        return debug_return('paywall', f"Paywall signals detected (count={sum(signals)})")

    # 3. Email vs Input
    if has_visible_inputs:
        is_em = input_state.get('is_email', False)
        is_nm = input_state.get('is_name', False)
        is_ag = input_state.get('is_age', False)
        
        # If it explicitly says email in the field, it's email
        if is_em and not is_nm:
            return debug_return('email', "Email field found via attributes")
            
        # If it says name or age, it's input
        if is_nm or is_ag:
            return debug_return('input', "Name/Age field found")
            
        # Fallback to headings
        if any(k in heading_text for k in ["email", "e-mail", "mail"]):
            return debug_return('email', "Email context in heading")
            
        if any(k in heading_text for k in ["name", "age", "weight", "height", "call you"]):
            return debug_return('input', "Input context in heading")
            
        if "email" in u and "magic-page" not in u:
            return debug_return('email', "Email in URL")
            
        return debug_return('input', f"{input_state['count']} input(s) found without explicit email signals")

    # 4. Game/Prize/Spin screens (should be info)
    game_kws = ["spin", "wheel", "prize"]
    if any(k in t for k in game_kws) or "prize-wheel" in u:
        btn_text = (page.evaluate('() => Array.from(document.querySelectorAll(\'button, a, [role="button"]\')).map(el => el.innerText).join(" ")') or "").lower()
        if any(k in btn_text for k in game_kws) or "prize-wheel" in u:
            return debug_return('info', "Game/Prize screen detected (keyword in buttons/URL)")

    # 4.5 Email Consent / Notifications (Should be question)
    consent_kws = ["receive emails", "send me emails", "notifications", "updates", "stay in the loop", "newsletters", "consent", "i'm in"]
    if any(k in t for k in consent_kws) and ("email" in t or "mail" in t or "email-page" in u):
        return debug_return('question', "Email consent/notification screen detected")

    # 5. Question vs Info (Smart separation)
    nav_words = [
        "next", "continue", "skip", "back", "weiter", "zurück", "next step", "proceed", 
        "got it", "ok", "okay", "great", "understood", "yes", "i'm in", "let's go", 
        "see results", "start", "принять", "ок", "начать", "понятно", "хорошо",
        "do it", "ready", "begin", "let's", "go", "transformation"
    ]
    
    raw_els = page.locator("[data-testid*='answer' i]:visible, [class*='Card' i]:not([class*='testimonial' i]):not([class*='review' i]):visible, label:visible, button:visible, a:visible, [role='button']:visible, div[role='button']:visible")
    choices = []
    nav_btns = []
    
    for i in range(raw_els.count()):
        el = raw_els.nth(i)
        if is_forbidden_button(el): continue

        is_int = el.evaluate("""el => {
            const t = el.tagName.toLowerCase();
            if (t === 'button' || t === 'a' || t === 'label' || t === 'input') return true;
            if (el.getAttribute('role') === 'button') return true;
            return window.getComputedStyle(el).cursor === 'pointer';
        }""")
        if not is_int: continue

        txt = (el.inner_text() or "").lower().strip()
        
        if not txt:
            tag = el.evaluate("el => el.tagName").lower()
            if tag in ['button', 'label', 'input']:
                choices.append("empty_choice")
            continue

        is_nav = False
        for w in nav_words:
            if txt == w or txt.startswith(w + "!") or txt.startswith(w + ".") or (len(txt) < 20 and w in txt):
                is_nav = True
                break
        
        if is_nav:
            nav_btns.append(txt)
        else:
            choices.append(txt)

    total_interactive = len(choices) + len(nav_btns)

    has_skip = any("skip" in b for b in nav_btns)
    has_picker = False
    try:
        has_picker = page.locator("select:visible, [role='combobox']:visible, [role='listbox']:visible, [role='slider']:visible, [class*='picker' i]:visible").count() > 0
    except: pass

    if total_interactive == 0:
        return debug_return('other', "No clear type detected")
        
    if total_interactive == 1:
        if has_picker:
            return debug_return('question', "Single CTA but custom picker/selector found")
        return debug_return('info', "Single interactive element detected, classifying as info")
        
    if total_interactive >= 2:
        if len(choices) >= 1 or has_skip or has_picker:
            return debug_return('question', f"Multiple options detected ({total_interactive}), including choices/skip/picker")
        else:
            return debug_return('info', f"Multiple nav buttons ({total_interactive}) but no choices, classifying as info")

    return debug_return('other', "No clear type detected")

def is_element_in_viewport(el: Page) -> bool:
    try:
        return el.evaluate("""el => {
            const rect = el.getBoundingClientRect();
            const windowWidth = window.innerWidth || document.documentElement.clientWidth;
            const windowHeight = window.innerHeight || document.documentElement.clientHeight;
            const style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.visibility === 'hidden') return false;
            
            // Check ancestors for opacity 0
            let parent = el.parentElement;
            while (parent) {
                const pStyle = window.getComputedStyle(parent);
                if (pStyle.opacity === '0' || pStyle.visibility === 'hidden') return false;
                parent = parent.parentElement;
            }
            
            // Allow elements that are partially visible in viewport (e.g. large tiles)
            return rect.width > 0 && rect.height > 0 && rect.left < windowWidth && rect.right > 0 && rect.top < windowHeight && rect.bottom >= 0;
        }""")
    except: return False

def find_continue_button(page: Page, log_func=None):
    # Always prioritize "Got it" modals first, regardless of element type
    got_it_btns = page.get_by_text("Got it", exact=False)
    for i in reversed(range(got_it_btns.count())):
        btn = got_it_btns.nth(i)
        if btn.is_visible(timeout=100) and is_element_in_viewport(btn):
            if not is_forbidden_button(btn, log_func): return btn

    # Find the last button in the DOM that matches any of our keywords
    try:
        selector = "button, [role='button'], a.button, a, div[class*='button' i], div[class*='btn' i], span[class*='btn' i]"
        idx = page.evaluate(f"""(sel) => {{
            const keywords = ['close', 'continue', 'next', 'get my plan', 'start', 'take the quiz', 'get started', 'start quiz', 'get my offer', 'next step', 'proceed', 'submit', 'show my results', 'see my results', "i'm in"];
            const els = Array.from(document.querySelectorAll(sel));
            for (let i = els.length - 1; i >= 0; i--) {{
                const el = els[i];
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                
                const txt = (el.innerText || '').toLowerCase();
                if (!txt) continue;
                
                if (keywords.some(kw => txt.includes(kw)) && txt.length < 50) {{
                    // Check if it's forbidden
                    if (txt.includes('terms') || txt.includes('privacy') || txt.includes('policy') || txt.includes('back') || txt.includes('назад') || txt.includes('login') || txt.includes('log in')) continue;
                    return i;
                }}
            }}
            return -1;
        }}""", selector)
        
        if idx >= 0:
            return page.locator(selector).nth(idx)
    except Exception as e:
        pass

    return None

def wait_for_transition(page: Page, old_url: str, old_hash: str, timeout=10.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            page.wait_for_load_state('networkidle', timeout=500)
        except: pass
        if page.url != old_url or get_screen_hash(page) != old_hash: return True
        time.sleep(0.5)
    return False

NAV_KEYWORDS = [
    "next", "continue", "skip", "back", "weiter", "zurück", "next step", "proceed", 
    "got it", "ok", "принять", "ок", "start", "get started", "take the quiz", 
    "accept", "allow", "agree", "alle akzeptieren", "alle ablehnen"
]

def is_nav_button(text: str) -> bool:
    if not text: return False
    t = text.lower().strip()
    return any(w == t or t.startswith(w + " ") for w in NAV_KEYWORDS)

def perform_action(page: Page, screen_type: str, log_func, results_dir: str, start_hash: str, start_url: str):
    try:
        if screen_type == 'paywall': return "stopped at paywall"
        if screen_type == 'checkout': return "checkout reached"

        if screen_type == 'loading':
            log_func("Loading screen detected. Waiting for semantic transition (URL or UI change)...")
            t_start = time.time()
            transitioned = False
            # Wait up to 25 seconds for loading to finish naturally
            while time.time() - t_start < 25.0:
                try: page.wait_for_load_state('networkidle', timeout=500)
                except: pass
                
                # Check 1: Did URL change?
                if page.url != start_url:
                    transitioned = True
                    break
                    
                # Check 2: Re-classify silently. If it's no longer 'loading' (e.g., buttons appeared), we are done
                current_st = classify_screen(page, log_func=None)
                if current_st != 'loading':
                    transitioned = True
                    break
                    
                time.sleep(1.0)
                
            if not transitioned:
                log_func("Loading transition timeout. Forcing click...")
                try:
                    # In coursiv, sometimes the loading is actually a button
                    btn = page.locator("button:visible, [role='button']:visible, a.button:visible").first
                    if btn.count() > 0: btn.click(force=True, timeout=1000)
                except: pass
                time.sleep(3.0)
            return "loading_waited"

        if screen_type in ['email', 'input']:
            checked = ensure_privacy_checkbox_checked(page, log_func)
            if checked: log_func("privacy_policy_checked=true")
            
            # Wait for inputs to be visible
            try: page.wait_for_selector("input:visible, textarea:visible", timeout=3000)
            except: pass
            
            inputs = page.locator("input:visible, textarea:visible")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                itype = (inp.get_attribute("type") or "text").lower()
                placeholder = (inp.get_attribute("placeholder") or "").lower()
                name = (inp.get_attribute("name") or "").lower()
                
                context_text = (page.evaluate("() => document.body.innerText") or "").lower()
                context_url = page.url.lower()
                
                # Use word boundaries for numeric keywords to avoid false positives like 'age' in 'magic-page'
                num_pattern = r'\b(age|height|weight|возраст|рост|вес|bmi|goal|цель)\b'
                is_numeric = itype == "number" or re.search(num_pattern, placeholder) or re.search(num_pattern, context_url)
                
                val = "John"
                if itype == "range": val = "50"
                if is_numeric:
                    if any(k in placeholder or k in context_url or k in context_text for k in ["height", "рост"]): 
                        val = "170" if i == 0 else "5" # For cm it uses first, for ft/in it fills 5 and 5
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["goal", "цель"]): val = "60"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["weight", "вес"]): val = "70"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["age", "возраст"]): val = "30"
                    else: val = "25"
                else:
                    if screen_type == 'email' or itype in ["email"] or any(k in placeholder or k in name for k in ["email", "e-mail", "mail@"]):
                        val = "yegor-pestov@list.ru"
                    elif any(k in placeholder or k in context_url or k in context_text for k in ["date", "dob", "birth"]):
                        val = "01011990"
                    else: val = "John"
                
                try:
                    log_func(f"Filling input: itype={itype}, val={val}")
                    if itype == "range":
                        inp.evaluate(f'(el, v) => {{ el.value = v; el.dispatchEvent(new Event("input", {{ bubbles: true }})); el.dispatchEvent(new Event("change", {{ bubbles: true }})); }}', val)
                    else:
                        inp.focus()
                        if val == "01011990":
                            page.keyboard.type(val, delay=50)
                        else:
                            inp.fill(val)
                        # Press enter directly on the input element
                        inp.press("Enter")
                        page.keyboard.press("Tab") # Blur input to trigger validation
                    time.sleep(0.3)
                except Exception as e:
                    log_func(f"Fill error: {str(e)[:50]}")
            
            # Force scroll to bottom to ensure all buttons/terms are loaded
            try: page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except: pass
            time.sleep(0.5)

            # Force check any checkboxes on the screen just in case
            try:
                cboxes = page.locator("input[type='checkbox']")
                for i in range(cboxes.count()):
                    cb = cboxes.nth(i)
                    is_checked = cb.evaluate("el => el.checked")
                    if not is_checked:
                        try: cb.evaluate("el => { el.click(); el.checked = true; el.dispatchEvent(new Event('change', {bubbles:true})); }")
                        except: pass
            except: pass

            # If there's a specific submit/continue button, click it, else press Enter
            btn = find_continue_button(page, log_func)
            if btn:
                try: btn_txt = " ".join((btn.evaluate("el => el.innerText") or "").split())
                except: btn_txt = "Continue"
                log_func(f"Clicking continue: {btn_txt}")
                close_popups(page, log_func)
                try:
                    btn.evaluate("el => el.scrollIntoView({block: 'center'})")
                    time.sleep(0.3)
                except: pass
                
                try: btn.click(timeout=1000)
                except:
                    try: btn.click(force=True, timeout=1000)
                    except:
                        try: btn.evaluate("el => el.click()", timeout=1000)
                        except: pass
            else:
                try: page.locator("input:not([type='hidden']):visible").first.press("Enter")
                except: page.keyboard.press("Enter")
                
            log_func("Input submitted. Waiting for transition...")
            
            # Wait for either URL change or input disappearance
            start_wait = time.time()
            trans_success = False
            while time.time() - start_wait < 12.0:
                curr_url = page.url
                if urlparse(curr_url).path != urlparse(start_url).path:
                    # Ignore transitions to legal/policy pages as successes
                    if "legal." not in urlparse(curr_url).netloc:
                        trans_success = True; break
                try:
                    # Check if the specific input we filled is still visible
                    if not page.locator("input:not([type='hidden']):visible, textarea:visible").count(): 
                        trans_success = True; break
                except: 
                    trans_success = True; break
                time.sleep(0.5)
                
            if not trans_success:
                try:
                    err_txt = page.evaluate("() => document.body.innerText")
                    log_func(f"Input transition failed! Text: {err_txt[:500]}")
                    
                    # Try to click the continue button again just in case Enter failed
                    if btn:
                        # Try finding a button inside main content to avoid footer legal links
                        fallback_btn = page.locator("main button:visible, [class*='content' i] button:visible, #root button:visible").first
                        if fallback_btn.count() > 0:
                            fallback_btn.click(force=True, timeout=1000)
                        else:
                            btn.click(force=True, timeout=1000)
                        
                        start_wait = time.time()
                        while time.time() - start_wait < 5.0:
                            curr_url = page.url
                            if urlparse(curr_url).path != urlparse(start_url).path:
                                if "legal." not in urlparse(curr_url).netloc:
                                    break
                            time.sleep(0.5)
                except: pass
            return "input_submitted"

        if screen_type in ['question', 'info', 'other']:
            cont_btn = find_continue_button(page, log_func)
            
            # If a modal "Got it" or "OK" button is present, we must click it FIRST to unblock the UI!
            try: 
                cont_txt_lower = (cont_btn.evaluate("el => el.innerText", timeout=1000) or "").lower()
            except: 
                cont_txt_lower = ""
            
            if cont_btn and any(k in cont_txt_lower for k in ["got it", "close"]):
                try: c_txt = " ".join((cont_btn.evaluate("el => el.innerText") or "").split())
                except: c_txt = "Got it"
                log_func(f"Modal dismiss button found: {c_txt}. Clicking...")
                close_popups(page, log_func)
                try:
                    cont_btn.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=1000)
                    time.sleep(0.3)
                except: pass
                try: cont_btn.click(timeout=1000)
                except:
                    try: cont_btn.click(force=True, timeout=1000)
                    except:
                        try: cont_btn.evaluate("el => el.click()", timeout=1000)
                        except: pass
                wait_for_transition(page, start_url, start_hash)
                return "modal_dismissed"

            # For info screens, if we found a continue button, use it!
            if screen_type == 'info' and cont_btn:
                try: c_txt = " ".join((cont_btn.evaluate("el => el.innerText") or "").split())
                except: c_txt = "Continue"
                log_func(f"Info screen continue found: {c_txt}. Clicking...")
                close_popups(page, log_func)
                try:
                    cont_btn.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=1000)
                    time.sleep(0.3)
                except: pass
                try: cont_btn.click(timeout=1000)
                except:
                    try: cont_btn.click(force=True, timeout=1000)
                    except: pass
                wait_for_transition(page, start_url, start_hash)
                return "info_continue_pressed"

            # Priority to "Start" buttons
            if cont_btn and any(k in cont_txt_lower for k in ["start", "get my", "get started", "take the", "offer", "claim", "discount", "spin"]):
                try: c_txt = " ".join((cont_btn.evaluate("el => el.innerText") or "").split())
                except: c_txt = "Continue"
                log_func(f"Landing/Start button found: {c_txt}. Clicking...")
                close_popups(page, log_func)
                cont_btn.click(force=True, timeout=1000)
                wait_for_transition(page, start_url, start_hash)
                return "start_button_pressed"

            choice_sel = [
                "[data-testid*='answer' i]:visible", 
                "button:visible", 
                "[class*='Item' i]:visible", 
                "[class*='Card' i]:visible", 
                "label:visible"
            ]
            valid_targets = []
            # Pass 1: Try all selectors to find all valid choices WITH text (excluding Nav)
            for s in choice_sel:
                els = page.locator(s)
                for i in range(els.count()):
                    curr = els.nth(i)
                    if is_forbidden_button(curr, log_func) or not is_element_in_viewport(curr): continue
                    try: txt = (curr.evaluate("el => el.innerText", timeout=500) or "").strip()
                    except: txt = ""
                    if txt and not re.search(r'^\d+\s*/\s*\d+$', txt) and len(txt) < 100 and not is_nav_button(txt):
                        # Avoid clicking already checked items if possible
                        try:
                            cb = curr.locator("input[type='checkbox'], input[type='radio']")
                            if cb.count() > 0:
                                if cb.first.evaluate("el => el.checked", timeout=500): continue
                        except: pass
                        valid_targets.append(curr)
                if valid_targets: break

            # Pass 2: If no text choices found, try all selectors for empty choices
            # BUT if there is a continue button, prioritize that over empty choices ONLY IF it's enabled
            if not valid_targets:
                # If there's a continue button AND it's enabled, we don't need to look for empty choices
                if cont_btn and cont_btn.is_enabled():
                    pass
                else:
                    for s in choice_sel:
                        els = page.locator(s)
                        for i in range(els.count()):
                            curr = els.nth(i)
                            if is_forbidden_button(curr, log_func) or not is_element_in_viewport(curr): continue
                            try:
                                txt = (curr.evaluate("el => el.innerText", timeout=500) or "").strip()
                            except: txt = ""
                            if not txt:
                                tag = curr.evaluate("el => el.tagName").lower()
                                role = (curr.get_attribute("role") or "").lower()
                                if tag in ['button', 'input', 'label'] or role == 'button' or "card" in (curr.get_attribute("class") or "").lower():
                                    valid_targets.append(curr)
                        if valid_targets: break

            target = random.choice(valid_targets) if valid_targets else None

            if not target:
                if cont_btn:
                    try: c_txt = " ".join((cont_btn.evaluate("el => el.innerText") or "").split())
                    except: c_txt = "Continue"
                    log_func(f"No choices, clicking continue: {c_txt}")
                    close_popups(page, log_func)
                    pre_cont_hash = get_screen_hash(page)
                    try:
                        cont_btn.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=1000)
                        time.sleep(0.5)
                    except: pass

                    try: cont_btn.click(timeout=2000)
                    except:
                        try: cont_btn.click(force=True, timeout=2000)
                        except:
                            try: cont_btn.evaluate("el => el.click()", timeout=2000)
                            except: pass
                    
                    if not wait_for_transition(page, start_url, pre_cont_hash, timeout=3.0):
                        page.keyboard.press("Enter")
                        wait_for_transition(page, start_url, pre_cont_hash)
                    return "info_continue_pressed"
                return "no_choices_found"
                
            start_choices = get_choices_text(page, log_func)
            start_ui = get_ui_step(page)
            
            # 1. Click choice
            clean_target_text = " ".join((target.inner_text() or "").split())
            display_text = clean_target_text[:50] if clean_target_text else "<No text>"
            
            checked = ensure_privacy_checkbox_checked(page, log_func)
            if checked: log_func("privacy_policy_checked=true")

            log_func(f"Clicking choice: {display_text}")
            try:
                target.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=1000)
                time.sleep(0.3)
            except: pass
            close_popups(page, log_func)
            try:
                # First try regular click, if intercepted, it will fail and we can try force click
                target.click(timeout=1000)
            except Exception as e:
                try:
                    target.click(force=True, timeout=1000)
                except Exception as e2:
                    try: target.evaluate("el => el.dispatchEvent(new MouseEvent('click', {view: window, bubbles: true, cancelable: true}))", timeout=1000)
                    except:
                        try:
                            # Final fallback: physical mouse click
                            box = target.bounding_box()
                            if box:
                                page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                        except: log_func(f"Click error: {str(e2)[:50]}")
            
            # If it looks like a multiselect (checkboxes present), maybe it's just a regular question
            # But we need to know so we don't auto-advance just because a checkbox was toggled.
            checkbox_count = sum(1 for t in valid_targets if t.locator("input[type='checkbox']").count() > 0)
            is_multiselect_page = checkbox_count > 1 and len(valid_targets) > 2
            
            # Check if there is a continue button. If there is, choice changes shouldn't trigger auto-advance.
            # Also sometimes we click a choice and a continue button appears instantly, so we need to check again
            has_cont_btn = find_continue_button(page, log_func=None) is not None
            
            # Short wait for auto-advance or progress change
            log_func("Waiting for auto-transition...")
            start_wait = time.time()
            transitioned_auto = False
            
            while time.time() - start_wait < 2.0:
                curr_ui = get_ui_step(page)
                curr_choices = get_choices_text(page, log_func)
                
                url_changed = page.url != start_url
                hash_changed = get_screen_hash(page) != start_hash
                ui_changed = curr_ui != start_ui
                choices_changed = start_choices and curr_choices and curr_choices != start_choices
                
                # Update has_cont_btn status in real-time in case it appeared
                if not has_cont_btn:
                    has_cont_btn = find_continue_button(page, log_func=None) is not None
                
                # In multiselect or pages with a continue button, choice text might change (e.g. checkmarks/active state) but we shouldn't auto-advance
                if url_changed or hash_changed or (ui_changed and curr_ui != "unknown") or (choices_changed and not is_multiselect_page and not has_cont_btn):
                    log_func(f"Auto-transition detected (ui_step: {start_ui} -> {curr_ui}, hash_changed: {hash_changed})")
                    transitioned_auto = True
                    break
                time.sleep(0.3)
            
            if transitioned_auto:
                return "auto_advanced"

            # 2. Wait for Next button if no auto-advance
            start_time = time.time()
            clicked_continue = False
            while time.time() - start_time < 3.0:
                curr_choices = get_choices_text(page, log_func)
                curr_ui = get_ui_step(page)
                choices_changed = start_choices and curr_choices and curr_choices != start_choices
                has_cont_btn_now = find_continue_button(page) is not None

                # If the UI updated or URL changed, we're good
                if (curr_ui != start_ui and curr_ui != "unknown") or (page.url != start_url) or (get_screen_hash(page) != start_hash):
                    break

                # If choices changed and there is NO continue button, it's a multiselect waiting for more clicks, so break to loop.
                # BUT if there is a continue button, we MUST try to click it instead of breaking!
                if choices_changed and not is_multiselect_page and not has_cont_btn_now:
                    break

                curr_cont = find_continue_button(page, log_func)
                if curr_cont and (curr_cont.is_enabled() or not is_multiselect_page):
                    try: c_txt = " ".join((curr_cont.evaluate("el => el.innerText") or "").split())
                    except: c_txt = "Continue"
                    is_en = curr_cont.is_enabled()
                    log_func(f"Continue button found: {c_txt} (enabled: {is_en}). Clicking...")

                    pre_cont_hash = get_screen_hash(page)

                    if is_en:
                        try:
                            curr_cont.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=1000)
                            time.sleep(0.5)
                        except: pass

                        try: curr_cont.click(timeout=2000)
                        except:
                            try: curr_cont.click(force=True, timeout=2000)
                            except:
                                try: curr_cont.evaluate("el => el.click()", timeout=2000)
                                except: pass

                        # Check if transition happened already to avoid double-clicking and 30s hangs
                        if not wait_for_transition(page, page.url, pre_cont_hash, timeout=3.0):
                            # Extra click using raw DOM for stubborn React forms like MadMuscles email consent
                            try: curr_cont.evaluate("el => el.dispatchEvent(new MouseEvent('click', {view: window, bubbles: true, cancelable: true}))", timeout=2000)
                            except: pass
                    else:
                        log_func("Continue button is disabled, skipping click.")

                    # Only press Enter if we STILL haven't transitioned
                    if not wait_for_transition(page, page.url, pre_cont_hash, timeout=3.0):
                        page.keyboard.press("Enter")
                        wait_for_transition(page, page.url, pre_cont_hash, timeout=10.0)

                    clicked_continue = True
                    return "continue_clicked"
                time.sleep(0.5)
            # 3. Multiselect
            if not clicked_continue:
                curr_cont = find_continue_button(page, log_func)
                if curr_cont and not curr_cont.is_enabled():
                    log_func(f"Multiselect detected. valid_targets: {len(valid_targets)}")
                    if valid_targets:
                        # Try to click unselected valid options
                        for curr in valid_targets[:5]:
                            if curr == target: continue
                            if is_forbidden_button(curr, log_func): continue
                            if curr_cont.is_enabled(): break
                            
                            is_already_checked = False
                            try:
                                cb = curr.locator("input[type='checkbox'], input[type='radio']")
                                if cb.count() > 0 and cb.first.evaluate("el => el.checked", timeout=500):
                                    is_already_checked = True
                            except: pass
                            
                            if is_already_checked: continue

                            try:
                                close_popups(page, log_func)
                                try: curr.evaluate("el => el.scrollIntoView({block: 'center'})")
                                except: pass
                                try: curr.evaluate("el => el.click()", timeout=1000)
                                except: curr.click(force=True, timeout=500)
                                
                                if curr_cont.is_enabled():
                                    close_popups(page, log_func)
                                    pre_multi_hash = get_screen_hash(page)
                                    try: curr_cont.evaluate("el => el.scrollIntoView({block: 'center'})")
                                    except: pass
                                    try: curr_cont.evaluate("el => el.click()", timeout=1000)
                                    except: curr_cont.click(force=True, timeout=1000)
                                    wait_for_transition(page, page.url, pre_multi_hash, timeout=10.0)
                                    return "multiselect_completed"
                            except: pass

                    # If we exhausted all options and it still thinks it's disabled, try clicking it anyway!
                    close_popups(page, log_func)
                    pre_multi_hash = get_screen_hash(page)
                    try:
                        try:
                            curr_cont.evaluate("el => el.scrollIntoView({block: 'center'})")
                        except: pass
                        
                        try: 
                            curr_cont.evaluate("el => el.click()", timeout=1000)
                        except: 
                            curr_cont.click(force=True, timeout=1000)
                            
                        wait_for_transition(page, page.url, pre_multi_hash, timeout=10.0)
                        return "multiselect_force_completed"
                    except Exception as e: 
                        log_func(f"Force multiselect error: {e}")
                        pass
            wait_for_transition(page, start_url, start_hash, timeout=15.0)
            return "screen_interaction_completed"                
    except Exception as e: return f"err:{str(e)}"
    return "none"

def run_funnel(url: str, config: dict, is_headless: bool):
    def get_slug(u): 
        parsed = urlparse(u)
        base = re.sub(r'[^a-zA-Z0-9\-]', '', parsed.netloc + parsed.path.replace('/', '-'))
        if parsed.query:
            query_hash = hashlib.md5(parsed.query.encode('utf-8')).hexdigest()[:6]
            return f"{base}-{query_hash}"
        return base
        
    slug = get_slug(url)
    res_dir = os.path.join('results', slug)
    os.makedirs(res_dir, exist_ok=True)
    classified_dir = os.path.join('results', '_classified')
    for cat in ['question', 'info', 'input', 'email', 'paywall', 'other', 'checkout', 'loading']:
        os.makedirs(os.path.join(classified_dir, cat), exist_ok=True)
    summary = {"url": url, "slug": slug, "steps_total": 0, "paywall_reached": False, "last_url": "", "path": res_dir, "error": None}

    with open(os.path.join(res_dir, 'log.txt'), 'w', encoding='utf-8') as f:
        def log(m):
            l = f"[{time.strftime('%H:%M:%S')}] {m}\n"; f.write(l); print(l.strip())
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=is_headless, slow_mo=100)
            page = browser.new_context(**p.devices['iPhone 13']).new_page()
            log(f"Navigating to {url} (slug: {slug})"); page.goto(url, wait_until='load', timeout=60000)
            
            step, history = 1, []
            while step <= 80:
                curr_u = page.url

                # Wait for screen to stabilize before acting
                stab_hash = get_screen_hash(page)
                for _ in range(3):
                    time.sleep(0.5)
                    new_hash = get_screen_hash(page)
                    if new_hash == stab_hash: break
                    stab_hash = new_hash

                close_popups(page, log)
                curr_h = get_screen_hash(page)
                st = classify_screen(page, log)
                ui_before = get_ui_step(page)

                log(f"step:{step} | type:{st} | ui_step:{ui_before} | url:{curr_u[:80]}")

                curr_id = f"{curr_u}|{curr_h}"
                history.append(curr_id)

                # Failsafe for external/legal pages
                if "legal." in urlparse(curr_u).netloc:
                    log(f"Stuck on legal/policy page {curr_u}. Stopping.")
                    summary["error"] = "legal_page_trap"
                    break

                is_stuck = history.count(curr_id) == 4 or (st == 'loading' and history.count(curr_id) == 2)
                is_fatal = history.count(curr_id) > 5 or (st == 'loading' and history.count(curr_id) > 3)
                # Allow a few retries, but force an info fallback if stuck
                if is_stuck:
                    log("Stuck loop warning. Attempting to force continue...")

                    try:
                        # Try to find specific continue button first
                        btn = find_continue_button(page)
                        if not btn:
                            # Try to find any unclicked interactive element that isn't forbidden
                            btn = page.locator("button:visible, [role='button']:visible").first
                        if btn and btn.count() > 0:
                            try: btn.evaluate("el => el.scrollIntoView({block: 'center'})")
                            except: pass
                            time.sleep(0.5)
                            try: btn.evaluate("el => el.click()", timeout=1000)
                            except: btn.click(force=True, timeout=1000)
                    except: pass

                    page.keyboard.press("Enter")
                    wait_for_transition(page, curr_u, curr_h, timeout=5.0)
                    continue

                elif is_fatal:
                    log(f"Stuck at {curr_u}. Stopping."); summary["error"] = "stuck_loop"; break                
                if st in ['paywall', 'checkout']:
                    delay_ms = config.get('paywall_screenshot_delay_ms', 3000)
                    log(f"Paywall/Checkout detected. Waiting {delay_ms}ms before screenshot...")
                    time.sleep(delay_ms / 1000.0)
                
                screen_name = f"{step:02d}_{st}.png"
                local_path = os.path.join(res_dir, screen_name)
                page.screenshot(path=local_path, full_page=True)
                shutil.copy2(local_path, os.path.join(classified_dir, st, f"{slug}__{screen_name}"))
                
                log(f"step:{step} | type:{st} | ui_step:{ui_before} | url:{page.url[:60]}")
                act = perform_action(page, st, log, res_dir, curr_h, curr_u)
                
                # Check for double skip
                time.sleep(1)
                ui_after = get_ui_step(page)
                
                def parse_ui(s):
                    if not s or s == "unknown": return -1
                    m = re.match(r'(\d+)', s)
                    return int(m.group(1)) if m else -1
                
                ui_b_num = parse_ui(ui_before)
                ui_a_num = parse_ui(ui_after)
                
                if ui_a_num > ui_b_num + 1 and ui_b_num > 0:
                    log(f"skip_detected=true | UI jumped from {ui_before} to {ui_after}")
                
                log(f"action_result:{act}")
                
                summary["steps_total"] = step; summary["last_url"] = page.url
                if st in ['paywall', 'checkout'] or "stopped" in act or "reached" in act:
                    if st == 'paywall': summary["paywall_reached"] = True
                    break
                
                step += 1
            browser.close()
    return summary

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='config.json', help='Path to config file')
    parser.add_argument('--parallel', action='store_true', help='Run funnels in parallel threads')
    parser.add_argument('--headless', type=str, help='Override headless mode (true/false)')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = json.load(f)
    
    funnels = config.get('funnels', [])
    max_f = config.get('max_funnels')
    if max_f is not None:
        funnels = funnels[:max_f]
    
    headless = config.get('headless', True)
    if args.headless is not None:
        headless = args.headless.lower() == 'true'

    if args.parallel:
        print(f"\n--- Starting {len(funnels)} funnels in PARALLEL mode ---\n")
        with ThreadPoolExecutor(max_workers=len(funnels)) as executor:
            futures = [executor.submit(run_funnel, url, config, headless) for url in funnels]
            all_summaries = [f.result() for f in futures]
    else:
        print(f"\n--- Starting {len(funnels)} funnels in SEQUENTIAL mode ---\n")
        all_summaries = []
        for url in funnels:
            print(f"\n>> Processing: {url}")
            summary = run_funnel(url, config, headless)
            all_summaries.append(summary)
    
    with open(os.path.join('results', 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(all_summaries, f, indent=4)
    
    print("\nBatch run completed.")
    for s in all_summaries:
        status = "PASSED" if s['paywall_reached'] else "FAILED"
        print(f"[{status}] URL: {s['url']} | Steps: {s['steps_total']}")
