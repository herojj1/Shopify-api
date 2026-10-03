import requests
import re
import json
from urllib.parse import urlparse, parse_qs

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
})
#AQUI voce so bota o get, que geralmente eo anchor, e o reload, e pronto, bypass pronto, implementa no seu codigo
recaptcha_url_get = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=6LfSVtwpAAAAAKuMLvhRBnJeBdgj29CD0Q_mD27E&co=aHR0cHM6Ly93d3cuZGllZ29iZXJ0b2xpbmkuY29tLmJyOjQ0Mw..&hl=pt-BR&v=XOqlk8PL_yVx6IdpLbpXdiLy&size=invisible&anchor-ms=20000&execute-ms=30000&cb=4thakzkw16df"
recaptcha_url_post = "https://www.google.com/recaptcha/api2/reload?k=6LfSVtwpAAAAAKuMLvhRBnJeBdgj29CD0Q_mD27E"

def extract_params_from_url(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return {k: v[0] if v else '' for k, v in params.items()}

def get_anchor_page():
    response = session.get(recaptcha_url_get)
    return response.text

def reload_recaptcha(token):
    params = extract_params_from_url(recaptcha_url_get)
    
    data = {
        'v': params.get('v', ''),
        'reason': 'q',
        'threat': '0',
        'c': token,
        'k': params.get('k', ''),
        'co': params.get('co', ''),
        'hl': params.get('hl', ''),
        'size': params.get('size', '')
    }
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://www.google.com/',
        'Origin': 'https://www.google.com'
    }
    
    response = session.post(recaptcha_url_post, data=data, headers=headers)
    return response.text

def extract_recaptcha_token(response_text):
    patterns = [
        r'"rresp","([^"]+)"',
        r'"token":"([^"]+)"',
        r'"recaptcha-token":"([^"]+)"',
        r'token=([^&]+)',
        r'response=([^&]+)',
        r'\["rresp","([^"]+)"\]'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, response_text)
        if match:
            return match.group(1)
    
    try:
        data = json.loads(response_text)
        if 'token' in data:
            return data['token']
        if 'rresp' in data:
            return data['rresp']
    except:
        pass
    
    token_pattern = r'[A-Za-z0-9_-]{100,}'
    matches = re.findall(token_pattern, response_text)
    if matches:
        return max(matches, key=len)
    
    return None

def solve_recaptcha():
    anchor_response = get_anchor_page()
    token = extract_recaptcha_token(anchor_response)
    reload_response = reload_recaptcha(token)
    new_token = extract_recaptcha_token(reload_response)
    return new_token if new_token else token

def get_recaptcha_response():
    return solve_recaptcha()

if __name__ == "__main__":
    token = get_recaptcha_response()

    print(token)
# E EM PHP FICARIA ASSIM

# $anchorr = 'https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6LdGvYUrAAAAAI_abdD5XyoA9hLTiOdSs9yUzzhN&co=aHR0cHM6Ly93d3cuZmxvcmljdWx0dXJhbWFyeWNsYXIuY29tLmJyOjQ0Mw..&hl=pt-BR&v=N67nZn4AqZkNcbeMu4prBgzg&size=invisible&anchor-ms=20000&execute-ms=30000&cb=c4n8qdcce6yx';

# $r1 = file_get_contents($anchorr);
# preg_match('/recaptcha-token" value="(.*?)"/', $r1, $matches);
# $token1 = isset($matches[1]) ? $matches[1] : '';

# $payload = http_build_query([
#     'v' => 'N67nZn4AqZkNcbeMu4prBgzg',
#     'reason' => 'q',
#     'c' => $token1,
#     'k' => '6LdGvYUrAAAAAI_abdD5XyoA9hLTiOdSs9yUzzhN',
#     'co' => 'aHR0cHM6Ly93d3cuZmxvcmljdWx0dXJhbWFyeWNsYXIuY29tLmJyOjQ0Mw..',
#     'hl' => 'pt-BR',
#     'size' => 'invisible',
# ]);

# $headers = [
#     'Content-Type: application/x-www-form-urlencoded',
#     'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0'
# ];

# $ch = curl_init();
# curl_setopt($ch, CURLOPT_URL, "https://www.google.com/recaptcha/enterprise/reload?k=6LdGvYUrAAAAAI_abdD5XyoA9hLTiOdSs9yUzzhN");
# curl_setopt($ch, CURLOPT_POST, true);
# curl_setopt($ch, CURLOPT_POSTFIELDS, $payload);
# curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
# curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
# curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
# curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
# $r2 = curl_exec($ch);

# preg_match('/"rresp","(.*?)"/', $r2, $matches);
# $token2 = isset($matches[1]) ? $matches[1] : 'null';
