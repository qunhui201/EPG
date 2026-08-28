import cloudscraper
import base64
import uuid
import datetime
import hashlib
import time
import json
import sys
import re
import warnings
import os
from urllib.parse import urljoin, urlparse, parse_qs, quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import requests
import logging

# 关闭所有警告和日志
warnings.filterwarnings("ignore")

# 配置日志
logging.basicConfig(level=logging.ERROR)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
log.disabled = True

# 默认配置
DEFAULT_USER_AGENT = "%E5%9B%9B%E5%AD%A3%E7%B7%9A%E4%B8%8A/4 CFNetwork/3826.500.131 Darwin/24.5.0"
DEFAULT_TIMEOUT = 30  # 增加超时时间
CHANNEL_DELAY = 1  # 增加频道之间的延迟时间（秒）
MAX_RETRIES = 1  # 最大重试次数

# 默认账号（可被环境变量覆盖）
DEFAULT_USER = os.environ.get('GTV_USER', '')
DEFAULT_PASS = os.environ.get('GTV_PASS', '')

# ========== 代理设置（支持 HTTP/HTTPS/SOCKS5/SOCKS5h） ==========
# 优先级：命令行 --proxy > 专用环境变量 > ALL_PROXY > http_proxy/https_proxy
# 支持的环境变量：
#   HTTP_PROXY / http_proxy
#   HTTPS_PROXY / https_proxy
#   SOCKS5_PROXY / socks5_proxy  (标准 SOCKS5，本地 DNS)
#   SOCKS5H_PROXY / socks5h_proxy (SOCKS5 with remote DNS)
#   ALL_PROXY / all_proxy（同时用于 http 和 https）
PROXY_SETTINGS = {
    'http': os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY', ''),
    'https': os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY', ''),
}

# 读取 SOCKS5 环境变量
socks5_proxy = os.environ.get('socks5_proxy') or os.environ.get('SOCKS5_PROXY', '')
socks5h_proxy = os.environ.get('socks5h_proxy') or os.environ.get('SOCKS5H_PROXY', '')

# 注意：先检查 socks5h，再检查 socks5，以便 socks5h 优先（如果同时设置）
if socks5h_proxy:
    # 确保协议前缀存在，如果没有则添加 socks5h://
    if not socks5h_proxy.startswith(('socks5h://', 'socks5://')):
        socks5h_proxy = f'socks5h://{socks5h_proxy}'
    PROXY_SETTINGS['http'] = socks5h_proxy
    PROXY_SETTINGS['https'] = socks5h_proxy
elif socks5_proxy:
    # 确保协议前缀存在，如果没有则添加 socks5://
    if not socks5_proxy.startswith(('socks5://', 'socks5h://')):
        socks5_proxy = f'socks5://{socks5_proxy}'
    PROXY_SETTINGS['http'] = socks5_proxy
    PROXY_SETTINGS['https'] = socks5_proxy

# ALL_PROXY 兜底（同样保留原始协议）
all_proxy = os.environ.get('all_proxy') or os.environ.get('ALL_PROXY', '')
if all_proxy and not (PROXY_SETTINGS['http'] or PROXY_SETTINGS['https']):
    # 自动补全协议头（如果缺失，默认 http://）
    if not re.match(r'^(http|https|socks5|socks5h)://', all_proxy):
        all_proxy = f'http://{all_proxy}'
    PROXY_SETTINGS['http'] = all_proxy
    PROXY_SETTINGS['https'] = all_proxy

# 内存缓存
cache_play_urls = {}
CACHE_EXPIRATION_TIME = 86400  # 24小时有效期


def is_github_actions():
    """检查是否在 GitHub Actions 环境中运行"""
    return os.environ.get('GITHUB_ACTIONS') == 'true'


def get_proxies():
    """从环境变量或命令行参数获取代理设置，完全保留用户指定的协议（socks5 和 socks5h 分开）"""
    proxies = {}
    for scheme in ('http', 'https'):
        proxy_url = PROXY_SETTINGS.get(scheme, '')
        if proxy_url:
            # 确保有协议头
            if not re.match(r'^(http|https|socks5|socks5h)://', proxy_url):
                proxy_url = f'http://{proxy_url}'
            proxies[scheme] = proxy_url

    if proxies:
        for k, v in proxies.items():
            print(f"🔌 代理 {k}: {v}")
    else:
        if is_github_actions():
            print("🔌 GitHub Actions 环境中未设置代理，使用直接连接")
        else:
            print("🔌 未设置代理，使用直接连接")

    return proxies if proxies else None


def test_proxy_connection(scraper, timeout=10):
    """测试代理连接是否正常（支持 SOCKS5/SOCKS5h）"""
    try:
        test_url = "https://httpbin.org/ip"
        response = scraper.get(test_url, timeout=timeout)
        if response.status_code == 200:
            print("✅ 代理连接测试成功")
            return True
        else:
            print(f"⚠️ 代理连接测试失败，状态码: {response.status_code}")
            return False
    except Exception as e:
        print(f"⚠️ 代理连接测试失败: {e}")
        return False


def create_scraper_with_proxy(ua):
    """创建带有代理设置的 scraper（支持 socks5 和 socks5h 分开）"""
    scraper = cloudscraper.create_scraper()
    scraper.headers.update({"User-Agent": ua})

    # 设置代理
    proxies = get_proxies()
    if proxies:
        try:
            scraper.proxies.update(proxies)

            # 在非 GitHub Actions 环境中测试代理连接
            if not is_github_actions():
                if not test_proxy_connection(scraper):
                    print("⚠️ 代理连接测试失败，将使用直接连接")
                    scraper.proxies.clear()
        except Exception as e:
            print(f"⚠️ 代理设置失败: {e}，将使用直接连接")
            scraper.proxies.clear()

    return scraper


def generate_uuid(user):
    """根据账号和当前日期生成唯一 UUID，确保不同用户每天 UUID 不同"""
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    name = f"{user}-{today}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name)).upper()


def generate_4gtv_auth():
    head_key = "PyPJU25iI2IQCMWq7kblwh9sGCypqsxMp4sKjJo95SK43h08ff+j1nbWliTySSB+N67BnXrYv9DfwK+ue5wWkg=="
    KEY = b"ilyB29ZdruuQjC45JhBBR7o2Z8WJ26Vg"
    IV = b"JUMxvVMmszqUTeKn"
    decoded = base64.b64decode(head_key)
    cipher = AES.new(KEY, AES.MODE_CBC, IV)
    decrypted = cipher.decrypt(decoded)
    pad_len = decrypted[-1]
    decrypted = decrypted[:-pad_len].decode('utf-8')
    today = datetime.datetime.utcnow().strftime('%Y%m%d')
    sha512 = hashlib.sha512((today + decrypted).encode()).digest()
    return base64.b64encode(sha512).decode()


def sign_in_4gtv(user, password, fsenc_key, auth_val, ua, timeout):
    url = "https://api2.4gtv.tv/AppAccount/SignIn"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "fsenc_key": fsenc_key,
        "fsdevice": "iOS",
        "fsversion": "3.2.8",
        "4gtv_auth": auth_val,
        "User-Agent": ua
    }
    payload = {"fsUSER": user, "fsPASSWORD": password, "fsENC_KEY": fsenc_key}
    scraper = create_scraper_with_proxy(ua)

    resp = scraper.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data.get("Data") if data.get("Success") else None


def get_all_channels(ua, timeout):
    """获取所有频道集合的频道，并去除重复频道"""
    channel_sets = [1, 4]  # 已知的频道集合ID
    all_channels = []
    seen_channel_ids = set()  # 用于跟踪已看到的频道ID

    for set_id in channel_sets:
        print(f"📡 正在获取频道集合 {set_id}...")
        url = f'https://api2.4gtv.tv/Channel/GetChannelBySetId/{set_id}/pc/L/V'
        headers = {"accept": "*/*", "origin": "https://www.4gtv.tv", "referer": "https://www.4gtv.tv/", "User-AAgent": ua}
        scraper = create_scraper_with_proxy(ua)

        try:
            resp = scraper.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("Success"):
                channels = data.get("Data", [])
                for channel in channels:
                    channel_id = channel.get("fs4GTV_ID", "")
                    # 检查是否已经处理过这个频道
                    if channel_id not in seen_channel_ids:
                        seen_channel_ids.add(channel_id)
                        all_channels.append(channel)
                        print(f"   ✅ 添加频道: {channel.get('fsNAME', '未知')}")
                    else:
                        print(f"   ⏭️  跳过重复频道: {channel.get('fsNAME', '未知')}")
        except Exception as e:
            print(f"   ❌ 获取频道集合 {set_id} 失败: {e}")
            continue

    return all_channels


def get_4gtv_channel_url_with_retry(channel_id, fnCHANNEL_ID, fsVALUE, fsenc_key, auth_val, ua, timeout, max_retries=MAX_RETRIES):
    """带重试机制的获取频道URL函数"""
    # 检查缓存
    current_time = time.time()
    cache_key = f"{channel_id}_{fnCHANNEL_ID}"
    if cache_key in cache_play_urls:
        cache_time, url = cache_play_urls[cache_key]
        if current_time - cache_time < CACHE_EXPIRATION_TIME:
            return url

    for attempt in range(max_retries):
        try:
            headers = {
                "content-type": "application/json; charset=utf-8",
                "fsenc_key": fsenc_key,
                "accept": "*/*",
                "fsdevice": "iOS",
                "fsvalue": "",
                "fsversion": "3.2.8",
                "4gtv_auth": auth_val,
                "Referer": "https://www.4gtv.tv/",
                "User-Agent": ua
            }
            payload = {
                "fnCHANNEL_ID": fnCHANNEL_ID,
                "clsAPP_IDENTITY_VALIDATE_ARUS": {"fsVALUE": fsVALUE, "fsENC_KEY": fsenc_key},
                "fsASSET_ID": channel_id,
                "fsDEVICE_TYPE": "mobile"
            }
            scraper = create_scraper_with_proxy(ua)

            resp = scraper.post('https://api2.4gtv.tv/App/GetChannelUrl2', headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get('Success') and 'flstURLs' in data.get('Data', {}):
                url = data['Data']['flstURLs'][1]
                # 更新缓存
                cache_play_urls[cache_key] = (current_time, url)
                return url
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"⚠️ 获取频道 {channel_id} 失败，正在重试 ({attempt + 1}/{max_retries})")
                time.sleep(2)  # 重试前等待2秒
            else:
                print(f"❌ 获取频道 {channel_id} 失败，已达到最大重试次数")
                return None
    return None


def get_highest_bitrate_url(master_url):
    """尝试获取更高质量的URL - 只对特定开头的网址进行处理"""
    # 只对以 "https://4gtvfree-mozai.4gtv.tv" 开头的网址进行处理
    if master_url.startswith("https://4gtvfree-mozai.4gtv.tv") and 'index.m3u8' in master_url:
        print(f"   📶 尝试获取高质量URL (1080p)...")
        return master_url.replace('index.m3u8', '1080.m3u8')

    # 对于其他网址，保持原样
    print(f"   📶 使用原始URL（非4gtvfree-mozai域名）")
    return master_url


def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=50, fill='█', print_end="\r"):
    """
    打印进度条
    @params:
        iteration   - 当前进度 (Int)
        total       - 总数 (Int)
        prefix      - 前缀字符串 (Str)
        suffix      - 后缀字符串 (Str)
        decimals    - 小数位数 (Int)
        length      - 进度条长度 (Int)
        fill        - 进度条填充字符 (Str)
        print_end   - 结束字符 (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=print_end)
    # 如果完成，打印新行
    if iteration == total:
        print()


def generate_m3u_playlist(user, password, ua, timeout, output_dir="playlist", delay=CHANNEL_DELAY):
    """生成M3U播放列表"""
    try:
        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)

        print("🔑 正在生成认证信息...")
        # 生成认证信息
        fsenc_key = generate_uuid(user)
        auth_val = generate_4gtv_auth()
        fsVALUE = sign_in_4gtv(user, password, fsenc_key, auth_val, ua, timeout)

        if not fsVALUE:
            print("❌ 登录失败")
            return False

        print("📡 正在获取频道列表...")
        # 获取所有频道
        channels = get_all_channels(ua, timeout)

        if not channels:
            print("❌ 无法获取频道列表")
            return False

        print(f"📺 共找到 {len(channels)} 个频道")

        # 创建M3U文件
        m3u_content = "#EXTM3U\n"
        successful_channels = 0
        failed_channels = 0
        failed_list = []

        # 显示进度条
        print("🚀 开始处理频道:")
        total_channels = len(channels)

        for index, channel in enumerate(channels):
            channel_id = channel.get("fs4GTV_ID", "")
            channel_name = channel.get("fsNAME", "")
            channel_type = channel.get("fsTYPE_NAME", "其他")
            channel_logo = channel.get("fsLOGO_MOBILE", "")
            fnCHANNEL_ID = channel.get("fnID", "")

            # 处理频道类型
            if channel_type:
                # 分割字符串并取第一部分
                channel_type = channel_type.split(',')[0]

            # 检查是否为fast-live开头，如果是则修改类型为FastTV飞速看
            if channel_id.startswith('fast-live'):
                channel_type = "FastTV飞速看"

            # 显示当前处理的频道信息
            print(f"\n[{index+1}/{total_channels}] 处理频道: {channel_name}")
            print(f"   📺 频道类型: {channel_type}")

            # 添加延迟
            time.sleep(delay)

            # 获取频道URL（带重试机制）
            try:
                print(f"   🔗 获取频道URL...")
                stream_url = get_4gtv_channel_url_with_retry(channel_id, fnCHANNEL_ID, fsVALUE, fsenc_key, auth_val, ua, timeout)
                if not stream_url:
                    print(f"   ❌ 无法获取频道 {channel_name} 的URL")
                    failed_channels += 1
                    failed_list.append((channel_name, "无法获取URL"))
                    continue

                # 尝试获取更高质量的URL（仅对特定域名）
                highest_url = get_highest_bitrate_url(stream_url)

                # 添加到M3U内容
                m3u_content += f'#EXTINF:-1 tvg-id="{channel_name}" tvg-name="{channel_name}" tvg-logo="{channel_logo}" group-title="{channel_type}",{channel_name}\n'
                m3u_content += f"{highest_url}\n"

                print(f"   ✅ 已添加频道: {channel_name}")
                successful_channels += 1

            except Exception as e:
                print(f"   ❌ 处理频道 {channel_name} 时出错: {e}")
                failed_channels += 1
                failed_list.append((channel_name, str(e)))
                continue

            # 更新进度条
            print_progress_bar(index + 1, total_channels, prefix='进度:', suffix=f'完成 {index+1}/{total_channels}')

        # 写入文件
        output_path = os.path.join(output_dir, "4gtv.m3u")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(m3u_content)

        print(f"\n🎉 播放列表生成完成: {output_path}")
        print(f"✅ 成功处理: {successful_channels} 个频道")
        print(f"❌ 失败处理: {failed_channels} 个频道")

        if failed_list:
            print("\n📋 失败频道清单:")
            for channel_name, error in failed_list:
                print(f"   - {channel_name}: {error}")

        return True

    except Exception as e:
        print(f"❌ 生成播放列表时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主函数，提供命令行界面"""
    import argparse

    parser = argparse.ArgumentParser(description='4GTV 流媒体获取工具')
    parser.add_argument('--generate-playlist', action='store_true', help='生成M3U播放列表')
    parser.add_argument('--user', type=str, default=DEFAULT_USER, help='用户名')
    parser.add_argument('--password', type=str, default=DEFAULT_PASS, help='密码')
    parser.add_argument('--ua', type=str, default=DEFAULT_USER_AGENT, help='用户代理')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help='超时时间(秒)')
    parser.add_argument('--output-dir', type=str, default="playlist", help='输出目录')
    parser.add_argument('--delay', type=float, default=CHANNEL_DELAY, help='频道之间的延迟时间(秒)')
    parser.add_argument('--retries', type=int, default=MAX_RETRIES, help='最大重试次数')
    parser.add_argument('--verbose', action='store_true', help='显示详细处理信息')
    parser.add_argument('--proxy', type=str, help='代理服务器（支持 http://, https://, socks5://, socks5h://）')
    parser.add_argument('--no-proxy', action='store_true', help='强制不使用代理')

    args = parser.parse_args()

    # 设置代理（命令行参数优先于环境变量）
    global PROXY_SETTINGS

    if args.no_proxy:
        PROXY_SETTINGS['http'] = ''
        PROXY_SETTINGS['https'] = ''
        print("🔌 强制禁用代理")
    elif args.proxy:
        proxy_url = args.proxy
        # 自动补全协议（如果用户只输入了 host:port，默认 http://）
        if not re.match(r'^(http|https|socks5|socks5h)://', proxy_url):
            proxy_url = f'http://{proxy_url}'
        PROXY_SETTINGS['http'] = proxy_url
        PROXY_SETTINGS['https'] = proxy_url
        print(f"🔌 使用命令行指定的代理: {proxy_url}")

    if args.generate_playlist:
        success = generate_m3u_playlist(
            args.user,
            args.password,
            args.ua,
            args.timeout,
            args.output_dir,
            args.delay
        )
        return 0 if success else 1
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
