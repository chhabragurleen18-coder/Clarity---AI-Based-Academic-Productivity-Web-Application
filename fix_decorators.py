import re

with open('c:/Users/hp/OneDrive/Desktop/antigravity/app.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Fix decorator order
code = code.replace('@login_required\n@app.route', '@app.route')
code = re.sub(r'(@app\.route\([^\)]+\)\n)', r'\1@login_required\n', code)

# But wait, we shouldn't add it to /login and /
code = code.replace("@app.route('/')\n@login_required", "@app.route('/')")
code = code.replace("@app.route('/login')\n@login_required", "@app.route('/login')")
# Same for api/auth
code = code.replace("@app.route('/api/auth/login', methods=['POST'])\n@login_required", "@app.route('/api/auth/login', methods=['POST'])")
code = code.replace("@app.route('/api/auth/register', methods=['POST'])\n@login_required", "@app.route('/api/auth/register', methods=['POST'])")
code = code.replace("@app.route('/api/auth/logout', methods=['POST'])\n@login_required", "@app.route('/api/auth/logout', methods=['POST'])")

with open('c:/Users/hp/OneDrive/Desktop/antigravity/app.py', 'w', encoding='utf-8') as f:
    f.write(code)
print("fixed decorators")
