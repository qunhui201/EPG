"""
LiTV EPG 抓取器 - GitHub Actions 專用版本
每日自動執行，生成 XMLTV 格式的節目表
"""

import sqlite3
import datetime
import os
import asyncio
import httpx
import zipfile
import io
import logging
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import List, Dict, Optional
import time
from pathlib import Path
import sys
import tempfile
import shutil

# 配置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # GitHub Actions 只需要控制台輸出
    ]
)

class LiTVEPGCrawler:
    def __init__(self, db_path: str = 'epg.db'):
        # 使用絕對路徑確保檔案位置正確
        self.db_path = os.path.join(os.getcwd(), db_path)
        self.epg_url = None
        self.headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; ABR-AL80 Build/4860264.0)",
            "Host": "proxy.svc.litv.tv",
            "Connection": "Keep-Alive"
        }
        # 設置上海時區 (UTC+8)
        self.shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))
        
    async def download_litv_epgs(self, force_update: bool = True) -> bool:
        """下載 LiTV EPG 資料庫（GitHub Actions 總是強制更新）"""
        
        logging.info("開始下載 LiTV EPG 資料庫...")
        logging.info(f"資料庫將儲存到: {self.db_path}")
        
        try:
            # 獲取 EPG SQLite URL
            data = json.dumps({
                "jsonrpc": "2.0",
                "id": 9527,
                "method": "ConfigService.GetConfigNoAuth",
                "params": {
                    "device_id": "0",
                    "swver": "LTAGP0231140LTV20250623101220",
                    "services": ["epg"]
                }
            })
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://proxy.svc.litv.tv/cdi/v2/rpc",
                    headers=self.headers,
                    data=data
                )
                response.raise_for_status()
                
                result = response.json()
                if "result" in result and "epg_sqlite" in result["result"]:
                    self.epg_url = result["result"]["epg_sqlite"][0]
                    logging.info(f"獲取到 EPG URL: {self.epg_url}")
                else:
                    logging.error("無法獲取 EPG URL")
                    return False
            
            # 下載 EPG 資料庫
            async with httpx.AsyncClient(timeout=120.0) as client:
                logging.info("正在下載 EPG 資料庫...")
                response = await client.get(self.epg_url)
                response.raise_for_status()
                
                # 解壓縮資料庫到臨時目錄
                with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
                    # 列出 ZIP 中的檔案
                    file_list = zip_file.namelist()
                    logging.info(f"ZIP 中包含的檔案: {file_list}")
                    
                    # 查找 .db 檔案
                    db_files = [f for f in file_list if f.endswith('.db')]
                    if not db_files:
                        logging.error("ZIP 中沒有找到 .db 檔案")
                        return False
                    
                    # 使用臨時目錄解壓縮
                    with tempfile.TemporaryDirectory() as tmpdir:
                        db_file_name = db_files[0]
                        logging.info(f"正在解壓 {db_file_name} 到臨時目錄")
                        
                        # 解壓縮到臨時目錄
                        zip_file.extract(db_file_name, tmpdir)
                        temp_db_path = os.path.join(tmpdir, db_file_name)
                        
                        # 檢查解壓縮是否成功
                        if not os.path.exists(temp_db_path):
                            logging.error(f"解壓縮失敗: {temp_db_path} 不存在")
                            return False
                        
                        # 移動檔案到目標位置
                        if os.path.exists(self.db_path):
                            os.remove(self.db_path)
                            logging.info(f"已刪除舊的資料庫檔案: {self.db_path}")
                        
                        shutil.move(temp_db_path, self.db_path)
                        logging.info(f"已移動資料庫檔案到: {self.db_path}")
            
            # 檢查最終檔案
            if os.path.exists(self.db_path):
                file_size = os.path.getsize(self.db_path)
                logging.info(f"EPG資料庫下載成功: {self.db_path} (大小: {file_size:,} bytes)")
                return True
            else:
                logging.error("資料庫檔案不存在，下載失敗")
                return False
            
        except httpx.RequestError as e:
            logging.error(f"網路請求失敗: {e}")
            return False
        except zipfile.BadZipFile:
            logging.error("下載的檔案不是有效的ZIP檔案")
            return False
        except Exception as e:
            logging.error(f"下載EPG資料庫時發生錯誤: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    def get_channels_litv(self) -> List[Dict]:
        """獲取所有頻道清單"""
        channels = []
        
        if not os.path.exists(self.db_path):
            logging.error(f"資料庫檔案不存在: {self.db_path}")
            return channels
        
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row  # 使用字典樣式的行
            cursor = conn.cursor()
            
            # 檢查是否有 channel_lineup 表
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='channel_lineup';")
            if not cursor.fetchone():
                logging.error("資料庫中沒有 channel_lineup 表")
                return channels
            
            # 從 channel_lineup 表獲取頻道信息
            cursor.execute("SELECT station_id, name, logo_mobile, cdn FROM channel_lineup ORDER BY station_id")
            rows = cursor.fetchall()
            
            logging.info(f"找到 {len(rows)} 個頻道")
            
            for row in rows:
                try:
                    station_id = row['station_id']
                    channel_name = row['name']
                    logo_mobile = row['logo_mobile']
                    cdn = row['cdn']
                    
                    # 構建完整的圖標 URL
                    if logo_mobile:
                        icon_url = f"https://p-cdnstatic.svc.litv.tv/pics/{logo_mobile}"
                    else:
                        icon_url = ""
                    
                    # 使用 cdn 作為頻道 ID
                    channel = {
                        'id': cdn,  # 使用 cdn 作為頻道 ID
                        'name': channel_name,
                        'station_id': station_id,
                        'cdn': cdn,
                        'icon': icon_url,
                        'source': 'litv'
                    }
                    
                    channels.append(channel)
                    
                except Exception as e:
                    logging.warning(f"解析頻道信息失敗: {e}")
                    continue
            
            conn.close()
            logging.info(f"成功獲取 {len(channels)} 個頻道")
            
        except Exception as e:
            logging.error(f"獲取頻道清單時發生錯誤: {e}")
            import traceback
            logging.error(traceback.format_exc())
        
        return channels
    
    def get_epgs_for_channel(self, channel: Dict, days: int = 7) -> Dict:
        """獲取指定頻道的節目表，默認7天"""
        epgs = []
        msg = ''
        success = 0
        
        if not os.path.exists(self.db_path):
            return {
                'success': 0,
                'epgs': [],
                'msg': f"資料庫檔案不存在: {self.db_path}",
                'ban': 0
            }
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 使用 station_id 構建表名
            station_id = channel.get('station_id')
            if not station_id:
                logging.warning(f"頻道 {channel.get('id')} 沒有 station_id 信息")
                return {
                    'success': 0,
                    'epgs': [],
                    'msg': "頻道沒有 station_id 信息",
                    'ban': 0
                }
            
            table_name = f"ch{station_id}"
            
            # 檢查表是否存在
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';")
            if not cursor.fetchone():
                logging.warning(f"頻道 {channel['name']} 的表 {table_name} 不存在")
                return {
                    'success': 0,
                    'epgs': [],
                    'msg': f"頻道表 {table_name} 不存在",
                    'ban': 0
                }
            
            # 計算時間範圍，從現在開始到指定天數後
            now = int(time.time() * 1000)
            start_time = now
            
            # 計算指定天數後的時間戳
            end_time = now + (days * 24 * 3600 * 1000)
            
            # 查詢指定天數內的節目數據
            cursor.execute(f"""
                SELECT station_id, title, start_time, end_time, description 
                FROM {table_name} 
                WHERE start_time >= ? AND start_time < ?
                ORDER BY start_time
            """, (start_time, end_time))
            
            rows = cursor.fetchall()
            
            for row in rows:
                try:
                    # 解析字段
                    station_id = row[0]
                    title = row[1]
                    start_time = row[2]
                    end_time = row[3]
                    description = row[4]
                    
                    # 轉換時間格式，使用上海時區
                    try:
                        # 先轉換為 UTC 時間，然後加上上海時區偏移
                        utc_dt = datetime.datetime.utcfromtimestamp(start_time / 1000)
                        start_dt = utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(self.shanghai_tz)
                        
                        utc_dt = datetime.datetime.utcfromtimestamp(end_time / 1000)
                        end_dt = utc_dt.replace(tzinfo=datetime.timezone.utc).astimezone(self.shanghai_tz)
                    except Exception as e:
                        logging.warning(f"時間轉換失敗: {e}")
                        continue
                    
                    epg = {
                        'channel_id': channel['id'],  # 使用 cdn 作為頻道 ID
                        'starttime': start_dt,
                        'endtime': end_dt,
                        'title': title,
                        'desc': description or ""
                    }
                    
                    epgs.append(epg)
                    
                except Exception as e:
                    logging.warning(f"解析節目數據時發生錯誤: {e}")
                    continue
            
            conn.close()
            success = 1
            msg = f"成功獲取 {len(epgs)} 個節目（{days}天）"
            
        except Exception as e:
            success = 0
            msg = f"獲取頻道 {channel.get('name')} 節目表時發生錯誤: {e}"
            logging.error(msg)
        
        return {
            'success': success,
            'epgs': epgs,
            'msg': msg,
            'ban': 0
        }
    
    def convert_to_xmltv(self, channels: List[Dict], epg_data: Dict, output_file: str = 'litv_epg.xml') -> bool:
        """將 EPG 數據轉換為 XMLTV 格式"""
        
        try:
            # 建立根元素
            tv = ET.Element('tv')
            tv.set('source-info-url', 'https://www.litv.tv/')
            tv.set('source-info-name', 'LiTV EPG')
            tv.set('generator-info-name', 'LiTV EPG Crawler')
            tv.set('generator-info-url', '')
            
            # 添加頻道信息
            for channel in channels:
                channel_elem = ET.SubElement(tv, 'channel')
                channel_elem.set('id', channel['id'])
                
                # 顯示名稱
                display_name = ET.SubElement(channel_elem, 'display-name')
                display_name.set('lang', 'zh')
                display_name.text = channel['name']
                
                # 圖標（如果有的話）
                if channel.get('icon'):
                    icon = ET.SubElement(channel_elem, 'icon')
                    icon.set('src', channel['icon'])
            
            # 添加節目信息
            program_count = 0
            for channel_id, programs in epg_data.items():
                if not programs:
                    continue
                    
                for program in programs:
                    programme = ET.SubElement(tv, 'programme')
                    
                    # 設置時間格式: YYYYMMDDHHMMSS +0800
                    start_str = program['starttime'].strftime('%Y%m%d%H%M%S +0800')
                    end_str = program['endtime'].strftime('%Y%m%d%H%M%S +0800')
                    
                    programme.set('start', start_str)
                    programme.set('stop', end_str)
                    programme.set('channel', channel_id)
                    
                    # 標題
                    title_elem = ET.SubElement(programme, 'title')
                    title_elem.set('lang', 'zh')
                    title_elem.text = program['title']
                    
                    # 描述
                    if program['desc']:
                        desc_elem = ET.SubElement(programme, 'desc')
                        desc_elem.set('lang', 'zh')
                        desc_elem.text = program['desc']
                    
                    program_count += 1
            
            # 美化 XML 輸出
            xml_str = ET.tostring(tv, encoding='utf-8', method='xml')
            dom = minidom.parseString(xml_str)
            pretty_xml = dom.toprettyxml(indent='  ', encoding='utf-8')
            
            # 寫入檔案
            with open(output_file, 'wb') as f:
                f.write(pretty_xml)
            
            logging.info(f"XMLTV 檔案生成成功: {output_file}")
            logging.info(f"包含 {len(channels)} 個頻道, {program_count} 個節目")
            
            return True
            
        except Exception as e:
            logging.error(f"生成 XMLTV 檔案時發生錯誤: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    async def run_full_crawl(self, output_file: str = 'litv_epg.xml', days: int = 7) -> bool:
        """運行完整的抓取流程，默認獲取7天數據"""
        
        logging.info(f"開始獲取 {days} 天的 LiTV EPG 數據...")
        
        # 1. 下載 EPG 資料庫
        if not await self.download_litv_epgs():
            logging.error("下載 EPG 資料庫失敗")
            return False
        
        # 2. 獲取頻道清單
        channels = self.get_channels_litv()
        if not channels:
            logging.error("無法獲取頻道清單")
            return False
        
        logging.info(f"開始獲取 {len(channels)} 個頻道的節目表...")
        
        # 3. 獲取每個頻道的節目表
        epg_data = {}
        total_programs = 0
        
        for i, channel in enumerate(channels, 1):
            if i % 10 == 0:
                logging.info(f"已處理 {i}/{len(channels)} 個頻道...")
            
            result = self.get_epgs_for_channel(channel, days)
            
            if result['success']:
                epg_data[channel['id']] = result['epgs']
                total_programs += len(result['epgs'])
            else:
                logging.warning(f"頻道 {channel['name']}: {result['msg']}")
            
            # 添加小延遲避免過快請求
            await asyncio.sleep(0.01)
        
        logging.info(f"總共獲取 {total_programs} 個節目")
        
        # 4. 轉換為 XMLTV 格式
        if self.convert_to_xmltv(channels, epg_data, output_file):
            logging.info("EPG 抓取和轉換完成!")
            return True
        else:
            logging.error("EPG 轉換失敗")
            return False


async def main():
    """主函數 - GitHub Actions 專用"""
    
    # 建立必要目錄
    output_dir = Path('output')
    output_dir.mkdir(exist_ok=True)
    
    # 建立抓取器實例
    crawler = LiTVEPGCrawler()
    
    # 設置輸出檔案路徑
    output_file = output_dir / 'litv.xml'
    
    # 運行完整抓取流程
    logging.info("開始運行 LiTV EPG 抓取器 (GitHub Actions)...")
    logging.info(f"將獲取7天節目表，輸出到 {output_file}")
    
    success = await crawler.run_full_crawl(
        output_file=str(output_file),
        days=7
    )
    
    if success:
        # 檢查輸出檔案大小
        if output_file.exists():
            file_size = output_file.stat().st_size
            logging.info(f"EPG 數據已儲存到 {output_file}")
            logging.info(f"檔案大小: {file_size:,} bytes ({file_size/1024/1024:.2f} MB)")
            
            # 讀取前幾行確認內容
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    lines = [next(f).strip() for _ in range(5)]
                    logging.info("XML 檔案前5行:")
                    for line in lines:
                        logging.info(f"  {line}")
            except Exception as e:
                logging.warning(f"讀取XML檔案失敗: {e}")
        else:
            logging.error("輸出檔案不存在!")
            return 1
        
        # 獲取頻道數量
        channels = crawler.get_channels_litv()
        logging.info(f"頻道數量: {len(channels)} 個")
        
        return 0
    else:
        logging.error("EPG 抓取失敗")
        return 1


if __name__ == '__main__':
    # 運行主程序
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
