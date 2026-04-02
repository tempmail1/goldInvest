import os
import requests
import pandas as pd
import time
import io
import json
import smtplib
import logging
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from datetime import date,datetime,timedelta

# ==========================================
# 📝 核心配置：专业日志系统 (双通道输出：控制台 + 文件)
# ==========================================
LOG_FILE = "sniper_run.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),  # 'a' 追加模式
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ==========================================
# ⚙️ 核心配置 (通过 GitHub Secrets 注入私密信息)
# ==========================================
class Config:
    SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.163.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
    SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
    AUTH_CODE = os.environ.get("AUTH_CODE", "")
    RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")
    FRED_KEY = os.environ.get("FRED_KEY", "")

    STATE_FILE = "sniper_state.json"
    MACRO_THRESHOLD = -0.5
    VIX_PANIC_LINE = 35.0


# ==========================================
# 🧠 本地状态记忆模块
# ==========================================
class LocalStateManager:
    def __init__(self):
        self.state_file = Config.STATE_FILE
        self.state = {
            "position": "NONE",
            "left_stop_price": 0.0,
            "current_date": "",
            "daily_alerts": []
        }
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
                logger.info("✅ 成功读取历史状态记忆。")
            except Exception as e:
                logger.warning(f"⚠️ 读取状态失败，使用默认初始化状态: {e}")
        else:
            logger.info("🆕 未发现历史状态，创建全新初始化状态。")

    def save_state(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=4)
            logger.info("💾 状态已更新至本地文件。")
        except Exception as e:
            logger.error(f"❌ 状态保存失败: {e}")


# ==========================================
# 🌐 引擎：底层数据抽水机
# ==========================================
class DataEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'
        })
        self.crumb = None
        self._get_cookie_and_crumb()

    def _get_cookie_and_crumb(self):
        logger.info("🔑 正在与 Yahoo 进行防风控底层握手...")
        try:
            self.session.get('https://fc.yahoo.com', timeout=10)
            res = self.session.get('https://query1.finance.yahoo.com/v1/test/getcrumb', timeout=10)
            if res.status_code == 200:
                logger.info(" 获取crumb成功")
                self.crumb = res.text.strip()
        except Exception as e:
            logger.error(f"❌ [与 Yahoo 进行防风控底层握手失败]: {e}")
            pass

    def get_yahoo_data(self, ticker, period="2y"):
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"range": period, "interval": "1d"}
        if self.crumb: params['crumb'] = self.crumb
        try:
            time.sleep(1)
            res = self.session.get(url, params=params, timeout=15)
            res.raise_for_status()
            result = res.json()['chart']['result'][0]
            timestamps = pd.to_datetime(result['timestamp'], unit='s').normalize()
            closes = result['indicators']['quote'][0]['close']

            df = pd.DataFrame({'Close': closes}, index=timestamps)
            df = df.dropna()
            # 剔除雅虎 API 偶尔返回的脏数据（重复日期）
            df = df[~df.index.duplicated(keep='last')]
            return df['Close']
        except Exception as e:
            logger.error(f"❌ [拉取失败] {ticker}: {e}")
            self._get_cookie_and_crumb()
            return None

    def get_fred_tips(self):
        try:
            api_key = Config.FRED_KEY

            url = "https://api.stlouisfed.org/fred/series/observations"
            start_date = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

            params = {
                "series_id": "DFII10",
                "api_key": api_key,
                "file_type": "json",
                "observation_start": start_date,
                "sort_order": "desc"
            }

            res = requests.get(url, params=params, timeout=15)
            res.raise_for_status()  # 检查 HTTP 状态码，如果不是 200 会抛出异常

            data = res.json()
            observations = data.get("observations", [])

            # 将 JSON 列表转换为 DataFrame
            df = pd.DataFrame(observations)

            # 只保留所需列，并重命名
            df = df[['date', 'value']].rename(columns={'value': 'DFII10'})

            # 将日期列转换为 datetime 格式，并设置为索引
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

            # 将数值转换为浮点数，errors='coerce' 会完美替代之前的 na_values='.'
            # 它会将所有无法转换为数字的字符（如 "."）安全地转换为 NaN
            df['DFII10'] = pd.to_numeric(df['DFII10'], errors='coerce')

            # 剔除包含 NaN 的行（周末、节假日）
            df = df.dropna()

            # 去重，保留最后一条，并返回 Pandas Series (与原代码行为一致)
            return df[~df.index.duplicated(keep='last')]['DFII10']

        except Exception as e:
            logger.error(f"❌ [拉取FRED API 数据失败]: {e}")
            return None


# ==========================================
# 🎯 中枢大脑
# ==========================================
class DualCoreSniper:
    def __init__(self, state_manager):
        self.engine = DataEngine()
        self.state_manager = state_manager
        self.sd = state_manager.state

    def calc_rsi(self, series, window=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def push_alert(self, title, content):
        logger.info(f"\n{'=' * 50}\n[{title}]\n{content}\n{'=' * 50}")

        if not all([Config.SENDER_EMAIL, Config.AUTH_CODE, Config.RECEIVER_EMAIL]):
            logger.warning("⚠️ 邮件发送失败：GitHub Secrets 中未配置完整的 SMTP 信息！")
            return

        try:
            # 1. 采用纯文本构建邮件正文 (去除 markdown 的 **)
            clean_content = content.replace("**", "")
            text_body = f"{clean_content}"

            msg = MIMEMultipart()

            # 2. 严格分离昵称和邮箱地址 (RFC 2822 标准)
            sender_name = str(Header("量化引擎雷达", 'utf-8'))
            receiver_name = str(Header("指挥官", 'utf-8'))
            msg['From'] = formataddr((sender_name, Config.SENDER_EMAIL))
            msg['To'] = formataddr((receiver_name, Config.RECEIVER_EMAIL))
            msg['Subject'] = Header(f"🚨 {title}", 'utf-8')

            # 附加纯文本载体
            msg.attach(MIMEText(text_body, 'plain', 'utf-8'))

            logger.info(f"📧 正在通过 {Config.SMTP_SERVER}:{Config.SMTP_PORT} 发送告警邮件...")
            server = smtplib.SMTP_SSL(Config.SMTP_SERVER, Config.SMTP_PORT)
            server.login(Config.SENDER_EMAIL, Config.AUTH_CODE)
            server.sendmail(Config.SENDER_EMAIL, [Config.RECEIVER_EMAIL], msg.as_string())
            server.quit()
            logger.info("✅ 告警邮件发送成功！")

        except Exception as e:
            logger.error(f"❌ 告警邮件发送失败: {e}")

    def run_scan(self):
        current_time = datetime.now().strftime('%H:%M:%S')
        today = datetime.now().strftime('%Y-%m-%d')

        # 每日重置日内防轰炸锁 (适配 JSON：将 set() 改为 [] 列表)
        # 注意：此处使用 self.sd 替代原有的 self.state 对象
        if self.sd.get('current_date') != today:
            self.sd['current_date'] = today
            self.sd['daily_alerts'] = []
            logger.info(f"🌅 新交易日 [{today}] 开启，阵地雷达已刷新。")

        logger.info(f"\n[{current_time}] 📡 启动 Z-Score 宏观扫描与均线核对...")

        # 1. 拉取数据 (保证充足的历史数据用于滚动计算)
        gold = self.engine.get_yahoo_data("GC=F", period="2y")
        tips = self.engine.get_fred_tips()
        dxy = self.engine.get_yahoo_data("DX-Y.NYB", period="1y")
        vix = self.engine.get_yahoo_data("^VIX", period="1y")

        if any(v is None for v in [gold, tips, dxy, vix]):
            logger.warning("⚠️ 数据链路异常，等待下一次扫描。")
            return

        # 2. 技术指标计算
        c_price = gold.iloc[-1]
        ma20 = gold.tail(20).mean()
        ma60 = gold.tail(60).mean()
        ma200 = gold.tail(200).mean()
        year_high = gold.tail(252).max()

        target_line = max(ma200, year_high * 0.80)
        rsi = self.calc_rsi(gold).iloc[-1]
        trend_up = ma20 > ma60

        # 3. 机构级连续宏观打分 (Z-Score)
        tips_mean, tips_std = tips.tail(60).mean(), tips.tail(60).std()
        dxy_mean, dxy_std = dxy.tail(60).mean(), dxy.tail(60).std()

        tips_z = (tips.iloc[-1] - tips_mean) / tips_std if tips_std != 0 else 0
        dxy_z = (dxy.iloc[-1] - dxy_mean) / dxy_std if dxy_std != 0 else 0

        macro_score = -0.6 * tips_z - 0.4 * dxy_z
        current_vix = vix.iloc[-1]

        # ---------------------------------
        # 🌟 仓位自我进化 (左转右)
        # ---------------------------------
        if self.sd.get('position', 'NONE') == 'LEFT' and c_price > ma60:
            self.sd['position'] = 'RIGHT'
            self.push_alert("🌟【仓位进化】护城河建立",
                            "左侧抄底仓位已成功站上季线 (MA60)。\n防守策略已切换为：跌破季线离场。")

        # ---------------------------------
        # 面板状态日志记录
        # ---------------------------------
        logger.info(f"▶ 现价: ${c_price:.2f} | 仓位状态: {self.sd.get('position', 'NONE')}")
        logger.info(
            f"▶ 均线: MA20 ${ma20:.2f} | MA60 ${ma60:.2f} | MA200 ${ma200:.2f} | 多头排列: {'✅' if trend_up else '❌'}")
        logger.info(
            f"▶ 宏观 Z-Score: {macro_score:.2f} (TIPS: {tips.iloc[-1]:.2f}%, DXY: {dxy.iloc[-1]:.2f}) (分界线 {Config.MACRO_THRESHOLD}) | VIX: {current_vix:.2f}")

        base_info = (f"现价: ${c_price:.2f} | RSI: {rsi:.2f}\n"
                     f"MA20: ${ma20:.2f} | MA60: ${ma60:.2f} | MA200 ${ma200:.2f} \n"
                     f"多头排列: {'是' if trend_up else '否'}\n"
                     f"宏观Z-Score: {macro_score:.2f} (TIPS: {tips.iloc[-1]:.2f}%, DXY: {dxy.iloc[-1]:.2f}) | VIX: {current_vix:.2f}")

        # ---------------------------------
        # 🚪 防守与离场决策
        # ---------------------------------
        if self.sd.get('position', 'NONE') != 'NONE':
            exit_reason = None
            if self.sd.get('position') == 'RIGHT' and c_price < ma60:
                exit_reason = "趋势彻底破位-跌破季线 MA60"
            elif self.sd.get('position') == 'LEFT' and c_price < self.sd.get('left_stop_price', 0.0):
                exit_reason = f"深渊防线被击穿 (跌破 ${self.sd.get('left_stop_price'):.2f})"

            if exit_reason and 'EXIT_CLEAR' not in self.sd['daily_alerts']:
                self.sd['daily_alerts'].append('EXIT_CLEAR')  # JSON 不支持 set，改用 list.append
                self.sd['position'] = 'NONE'  # 更新状态为空仓
                msg = f"📢 指令：【清仓撤退，转逆回购】\n触发原因: {exit_reason}。\n不要抱有幻想，严格执行纪律，保住本金等待下一次全仓机会。"
                self.push_alert("🚨【全仓清盘】防线崩溃预警", f"{msg}\n\n{base_info}")

        # ---------------------------------
        # 🔫 进攻与建仓决策 (⚠️已修复缩进 Bug，现在与防守逻辑平级)
        # ---------------------------------
        if 'EXIT_CLEAR' not in self.sd['daily_alerts']:
            target_exposure = 0.0
            new_type = self.sd.get('position', 'NONE')
            reason = ""

            # 【主策略】右侧顺势核弹
            if c_price > ma20 and trend_up and macro_score > Config.MACRO_THRESHOLD and current_vix < Config.VIX_PANIC_LINE:
                target_exposure = 0.80
                new_type = 'RIGHT'
                reason = "右侧主策略 (80%重仓跟随)"

            # 【机会策略】左侧深渊抄底
            elif c_price <= target_line and rsi <= 30 and macro_score > Config.MACRO_THRESHOLD:
                if self.sd.get('position') != 'RIGHT':
                    target_exposure = 0.40
                    new_type = 'LEFT'
                    reason = "左侧机会策略 (40%轻仓摸底)"
                    self.sd['left_stop_price'] = target_line * 0.95

            # 执行调仓动作
            if target_exposure > 0:
                if self.sd.get('position', 'NONE') == 'NONE' or (
                        self.sd.get('position') == 'LEFT' and new_type == 'RIGHT'):
                    if new_type not in self.sd['daily_alerts']:
                        self.sd['daily_alerts'].append(new_type)
                        self.sd['position'] = new_type

                        msg = f"📢 指令：【执行建仓/加仓，目标总仓位 {target_exposure * 100}%】\n触发逻辑：{reason}。\n不要犹豫，立刻按比例配置好仓位，然后关掉软件享受复利。"
                        self.push_alert("🚀【战术部署】仓位等级变动", f"{msg}\n\n{base_info}")

        # ---------------------------------
        # 🛌 静默状态播报
        # ---------------------------------
        if self.sd.get('position', 'NONE') == 'NONE' and not self.sd['daily_alerts']:
            logger.info("🛌 阵地静默。不在多头顺风区，安心让资金在 [逆回购] 里赚取年化复利。")
            self.push_alert("维持现状", f"阵地静默。不在多头顺风区，安心让资金在 [逆回购] 里赚取年化复利。\n\n{base_info}")
        elif self.sd.get('position', 'NONE') != 'NONE' and not self.sd['daily_alerts']:
            logger.info("🛡️ 重兵把守中。已经全仓在车上，关闭软件，享受利润奔跑，无视盘中洗盘。")

        # ==========================================
        # 💾 终极收尾：状态持久化 (GitHub Actions 核心)
        # ==========================================
        self.state_manager.save_state()


if __name__ == "__main__":
    logger.info("++++++🚀 GitHub Actions 定时任务启动++++++")
    sm = LocalStateManager()
    sniper = DualCoreSniper(sm)
    sniper.run_scan()
    logger.info("🏁 本次执行结束，容器即将销毁。")
