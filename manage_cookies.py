#!/usr/bin/env python3
"""
Cookie Management Utility for MusiQA Bot
Helps convert cookies and manage the cookie pool
"""

import os
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__)) if __file__.endswith('.py') else os.path.join(os.getcwd(), 'backend')
COOKIES_POOL_DIR = os.path.join(BACKEND_DIR, 'cookies_pool')
MAIN_COOKIES_FILE = os.path.join(BACKEND_DIR, 'cookies.txt')

def ensure_pool_dir():
    """Ensure cookies_pool directory exists"""
    os.makedirs(COOKIES_POOL_DIR, exist_ok=True)
    print(f"✓ Cookie pool directory: {COOKIES_POOL_DIR}")

def json_to_netscape(json_file: str, output_file: str = None):
    """Convert JSON cookies (from Browser extensions) to Netscape format"""
    if not os.path.exists(json_file):
        print(f"❌ File not found: {json_file}")
        return False
    
    output_file = output_file or MAIN_COOKIES_FILE
    
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
        
        if not isinstance(cookies, list):
            print(f"❌ Expected JSON array of cookies, got {type(cookies)}")
            return False
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("# Generated from JSON export\n")
            
            count = 0
            for cookie in cookies:
                domain = cookie.get('domain', '.youtube.com')
                flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = cookie.get('path', '/')
                secure = 'TRUE' if cookie.get('secure', False) else 'FALSE'
                
                # Set expiry to 1 year from now if not specified
                if cookie.get('expirationDate'):
                    expires = str(int(cookie['expirationDate']))
                else:
                    expires = str(int((datetime.now() + timedelta(days=365)).timestamp()))
                
                name = cookie.get('name', '')
                value = cookie.get('value', '')
                
                if name and value:
                    f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
                    count += 1
        
        print(f"✓ Converted {count} cookies: {json_file} → {output_file}")
        return True
    except json.JSONDecodeError:
        print(f"❌ Invalid JSON file: {json_file}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def create_cookie_pool(source_file: str = None, num_copies: int = 10):
    """Create multiple copies of a cookie file for pool rotation"""
    source_file = source_file or MAIN_COOKIES_FILE
    
    if not os.path.exists(source_file):
        print(f"❌ Source file not found: {source_file}")
        return 0
    
    if os.path.getsize(source_file) < 50:
        print(f"❌ Source file is too small ({os.path.getsize(source_file)} bytes)")
        return 0
    
    ensure_pool_dir()
    
    created = 0
    for i in range(num_copies):
        target_file = os.path.join(COOKIES_POOL_DIR, f"cookies_pool_{i}.txt")
        try:
            shutil.copy2(source_file, target_file)
            created += 1
            print(f"  ✓ Created: {os.path.basename(target_file)}")
        except Exception as e:
            print(f"  ❌ Failed: {os.path.basename(target_file)} - {e}")
    
    print(f"\n✓ Created {created}/{num_copies} cookie copies")
    return created

def clear_pool():
    """Clear all cookies from pool (useful for reset)"""
    if not os.path.exists(COOKIES_POOL_DIR):
        print("Pool directory doesn't exist")
        return
    
    count = 0
    for file in os.listdir(COOKIES_POOL_DIR):
        if file.endswith('.txt') and not file.startswith('.'):
            try:
                os.remove(os.path.join(COOKIES_POOL_DIR, file))
                print(f"  ✓ Deleted: {file}")
                count += 1
            except Exception as e:
                print(f"  ❌ Failed to delete {file}: {e}")
    
    # Also clear health data
    health_file = os.path.join(COOKIES_POOL_DIR, '.cookie_health.txt')
    if os.path.exists(health_file):
        try:
            os.remove(health_file)
            print(f"  ✓ Deleted health tracking data")
        except Exception as e:
            print(f"  ⚠️  Could not delete health data: {e}")
    
    print(f"\n✓ Cleared {count} cookie files")

def list_cookies():
    """List all available cookies"""
    print("\n📋 Available Cookies:\n")
    
    if os.path.exists(MAIN_COOKIES_FILE):
        size = os.path.getsize(MAIN_COOKIES_FILE)
        print(f"  Main: {MAIN_COOKIES_FILE} ({size} bytes)")
    
    if not os.path.exists(COOKIES_POOL_DIR):
        print("  Pool: (empty)")
        return
    
    files = sorted([f for f in os.listdir(COOKIES_POOL_DIR) 
                   if f.endswith('.txt') and not f.startswith('.')])
    
    if files:
        print(f"  Pool: {len(files)} files")
        for f in files[:20]:  # Show first 20
            size = os.path.getsize(os.path.join(COOKIES_POOL_DIR, f))
            print(f"    - {f} ({size} bytes)")
        if len(files) > 20:
            print(f"    ... and {len(files) - 20} more")
    else:
        print("  Pool: (empty)")

def verify_cookies(file_path: str):
    """Verify if a cookie file is valid for YouTube"""
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return False
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        has_youtube = any(domain in content for domain in 
                         ['.youtube.com', '.google.com', '.youtube-nocookie.com'])
        has_auth = any(token in content for token in 
                      ['LOGIN_INFO', 'SID', 'SAPISID', 'APISID', 'HSID', 'SSID'])
        
        print(f"\n🔍 Cookie Verification: {file_path}")
        print(f"  File size: {os.path.getsize(file_path)} bytes")
        print(f"  Has YouTube domain: {'✓' if has_youtube else '❌'}")
        print(f"  Has auth tokens: {'✓' if has_auth else '❌'}")
        
        if has_youtube and has_auth:
            print("  Status: ✓ VALID")
            return True
        else:
            print("  Status: ❌ INVALID - May not have proper YouTube authentication")
            return False
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        return False

def main():
    """Main menu"""
    print("\n" + "="*60)
    print("🎵 MusiQA Bot - Cookie Management Utility")
    print("="*60)
    
    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  python manage_cookies.py <command> [options]\n")
        print("Commands:")
        print("  convert <json_file>     Convert JSON cookies to Netscape format")
        print("  pool <source_file> [n]  Create n copies (default 10) in pool")
        print("  verify <file>           Verify if cookies are valid")
        print("  list                    List all available cookies")
        print("  clear                   Clear all pool cookies (DESTRUCTIVE)")
        print("\nExamples:")
        print("  python manage_cookies.py convert cookies.json")
        print("  python manage_cookies.py pool cookies.txt 15")
        print("  python manage_cookies.py verify cookies.txt")
        print("  python manage_cookies.py list")
        print("  python manage_cookies.py clear")
        return
    
    command = sys.argv[1].lower()
    
    if command == 'convert':
        if len(sys.argv) < 3:
            print("❌ Usage: python manage_cookies.py convert <json_file>")
            return
        json_to_netscape(sys.argv[2])
        # Also create pool copies
        if json_to_netscape(sys.argv[2]):
            response = input("\nCreate 10 copies in cookie pool? (y/n): ").strip().lower()
            if response == 'y':
                create_cookie_pool(MAIN_COOKIES_FILE, 10)
    
    elif command == 'pool':
        source = sys.argv[2] if len(sys.argv) > 2 else MAIN_COOKIES_FILE
        num = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        create_cookie_pool(source, num)
    
    elif command == 'verify':
        if len(sys.argv) < 3:
            print("❌ Usage: python manage_cookies.py verify <file>")
            return
        verify_cookies(sys.argv[2])
    
    elif command == 'list':
        list_cookies()
    
    elif command == 'clear':
        response = input("⚠️  This will delete all pool cookies. Continue? (y/n): ").strip().lower()
        if response == 'y':
            clear_pool()
    
    else:
        print(f"❌ Unknown command: {command}")
        print("Use: python manage_cookies.py (no args) for help")

if __name__ == '__main__':
    main()
