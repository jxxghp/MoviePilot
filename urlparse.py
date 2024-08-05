import sys
from urllib.parse import urlparse


def parse_url(url):
    parsed_url = urlparse(url)

    # 提取各个部分
    protocol = parsed_url.scheme or ""
    hostname = parsed_url.hostname or ""
    port = parsed_url.port or ""

    if hostname:
        hostname = hostname.lower()

    if not port:
        if protocol == "https":
            port = 443
        elif protocol == "http":
            port = 80
        elif protocol in {"socks5", "socks5h", "socks4", "socks4a"}:
            port = 1080

    if protocol:
        protocol = protocol.lower()

    # 打印提取的部分
    print(f"SCHEME:{protocol}")
    print(f"HOST:{hostname}")
    print(f"PORT:{port}")


if __name__ == "__main__":
    # 参数不全，直接返回空解析结果
    if len(sys.argv) != 2:
        print(f"SCHEME:''")
        print(f"HOST:''")
        print(f"PORT:''")
    # 参数全，解析URL
    else:
        PROXY_HOST = sys.argv[1]
        parse_url(url=PROXY_HOST)
