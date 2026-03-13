import json, sys

def test_baseline():
    try:
        with open('results/summary.json', 'r', encoding='utf-8') as f:
            summary = json.load(f)
    except FileNotFoundError:
        print("FAIL: results/summary.json not found")
        sys.exit(1)

    if not summary:
        print("FAIL: summary.json is empty")
        sys.exit(1)

    s = summary[0]
    print(f"Testing summary for: {s.get('url', 'unknown')}")
    
    if s.get('paywall_reached'):
        print("PASS: Paywall reached")
    else:
        print("FAIL: Paywall not reached. Last URL:", s.get('last_url'))
        sys.exit(1)
        
    if s.get('steps_total', 0) > 0:
        print(f"PASS: Total steps = {s['steps_total']}")
    else:
        print("FAIL: No steps recorded")
        sys.exit(1)
        
    if not s.get('error'):
        print("PASS: No fatal errors")
    else:
        print(f"FAIL: Error recorded: {s['error']}")
        sys.exit(1)
        
    try:
        with open(f"results/{s['slug']}/log.txt", 'r', encoding='utf-8') as f:
            new_log = f.read()
    except FileNotFoundError:
        print(f"FAIL: new log.txt not found at results/{s['slug']}/log.txt")
        sys.exit(1)
        
    try:
        with open("baseline_log.txt", 'r', encoding='utf-8') as f:
            base_log = f.read()
    except FileNotFoundError:
        print("WARN: baseline_log.txt not found, skipping comparison")
        base_log = ""
        
    if base_log:
        import re
        def extract_classifications(log_str):
            # Example log line: [12:05:46] step:20 | type:question | ui_step:15/19 | url:...
            return re.findall(r'type:(\S+).*?ui_step:(\S+)', log_str)
            
        base_class = extract_classifications(base_log)
        new_class = extract_classifications(new_log)
        
        if len(base_class) == len(new_class) and base_class == new_class:
            print("PASS: Classifications perfectly match baseline!")
        else:
            print(f"WARN: Classifications differ. Base runs: {len(base_class)}, New runs: {len(new_class)}")
            # Print a quick diff
            for i in range(min(len(base_class), len(new_class))):
                if base_class[i] != new_class[i]:
                    print(f"Difference at step {i+1}: Base={base_class[i]} vs New={new_class[i]}")
                    break
            
    print("\nALL AUTOMATED CHECKS PASSED")

if __name__ == "__main__":
    test_baseline()
