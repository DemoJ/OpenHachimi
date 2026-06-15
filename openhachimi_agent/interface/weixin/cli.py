"""独立的微信 iLink 扫码登录 CLI"""

import asyncio
import json
import sys
from pathlib import Path

from openhachimi_agent.interface.weixin.ilink_client import WeixinClient


def _get_base_dir() -> Path:
    """获取项目根目录的绝对路径。"""
    return Path(__file__).resolve().parents[3]


def _get_account_file() -> Path:
    """获取微信账号凭证文件的绝对路径。"""
    return _get_base_dir() / ".memory" / "weixin_account.json"


async def run_weixin_login():
    print("正在请求 Weixin iLink 登录二维码...")
    client = WeixinClient()
    account_file = _get_account_file()
    try:
        qr_data = await client.get_bot_qrcode()
        if qr_data.get("ret") != 0:
            print(f"获取二维码失败: {qr_data}")
            return

        qrcode_value = qr_data.get("qrcode")
        qrcode_url = qr_data.get("qrcode_img_content")
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value
        print("\n==============================================")
        print("请使用微信扫描下方链接中的二维码登录 (WeChat iLink):")
        if qrcode_url:
            print(qrcode_url)
        
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass
            
        print("==============================================\n")

        print("等待扫码中...")
        while True:
            await asyncio.sleep(2)
            status = await client.get_qrcode_status(qrcode_value)
            ret = status.get("ret", -1)
            status_str = status.get("status", "")
            
            if ret == 0:
                if status_str == "confirmed":
                    token = status.get("bot_token")
                    if not token:
                        print(f"❌ 登录异常，返回信息缺少 bot_token: {status}")
                        break
                    
                    print("\n✅ 微信登录成功！")
                    account_id = status.get("ilink_bot_id", "")
                    
                    account_file.parent.mkdir(parents=True, exist_ok=True)
                    data = {"token": token, "account_id": account_id}
                    account_file.write_text(json.dumps(data), "utf-8")
                    print(f"凭证已保存至 {account_file}")
                    print("如果 OpenHachimi 服务正在运行，微信渠道会自动检测并上线；否则请启动服务。")
                    break
                elif status_str == "scanned":
                    print("二维码已扫描，请在手机上确认授权...")
                elif status_str == "timeout" or status.get("err_msg") == "timeout":
                    print("❌ 二维码已过期，请重新运行此命令。")
                    break
                elif status_str == "canceled":
                    print("❌ 用户在手机上取消了登录。")
                    break
                elif status_str == "wait":
                    pass  # 继续等待
                else:
                    print(f"未知的二维码状态: {status}")
            elif ret == 1:
                pass  # 等待扫码
            else:
                print(f"❌ 登录异常，状态码非 0: {status}")
                break
    finally:
        await client.close()

def main():
    try:
        asyncio.run(run_weixin_login())
    except KeyboardInterrupt:
        print("\n已取消登录。")
        sys.exit(0)

if __name__ == "__main__":
    main()
