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
from datetime import datetime

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
            "gold_state": "NONE",
            "pool_state": "CASH",
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
        except:
            pass
        try:
            res = self.session.get('https://query1.finance.yahoo.com/v1/test/getcrumb', timeout=10)
            if res.status_code == 200: self.crumb = res.text.strip()
        except:
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
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"
            res = requests.get(url, timeout=15)
            df = pd.read_csv(io.StringIO(res.text), parse_dates=[0], index_col=0, na_values='.')
            df = df.dropna()
            return df[~df.index.duplicated(keep='last')]['DFII10']
        except:
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
            text_body = f"【告警标题】：{title}\n\n{clean_content}\n\n-----------------------------\n由 宏观双核量化系统 (GitHub Actions) 自动发送"

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
        today = datetime.now().strftime('%Y-%m-%d')

        if self.sd['current_date'] != today:
            self.sd['current_date'] = today
            self.sd['daily_alerts'] = []
            logger.info(f"🌅 新交易日 [{today}] 开启，状态重置。")

        logger.info("📡 启动双核宏观扫描...")

        gold = self.engine.get_yahoo_data("GC=F")
        qqq = self.engine.get_yahoo_data("QQQ")
        dia = self.engine.get_yahoo_data("DIA")
        tips = self.engine.get_fred_tips()
        dxy = self.engine.get_yahoo_data("DX-Y.NYB")
        vix = self.engine.get_yahoo_data("^VIX")

        if any(v is None for v in [gold, qqq, dia, tips, dxy, vix]):
            logger.warning("⚠️ 数据链路异常，放弃本次执行。")
            return

        gc_price, gc_ma20, gc_ma60 = gold.iloc[-1], gold.tail(20).mean(), gold.tail(60).mean()
        gc_target = max(gold.tail(200).mean(), gold.tail(252).max() * 0.80)
        gc_trend_up = gc_ma20 > gc_ma60
        gc_rsi = self.calc_rsi(gold).iloc[-1]

        qqq_price, qqq_ma60 = qqq.iloc[-1], qqq.tail(60).mean()
        dia_price, dia_ma60 = dia.iloc[-1], dia.tail(60).mean()

        tips_mean, tips_std = tips.tail(60).mean(), tips.tail(60).std()
        dxy_mean, dxy_std = dxy.tail(60).mean(), dxy.tail(60).std()
        macro_score = -0.6 * ((tips.iloc[-1] - tips_mean) / tips_std if tips_std else 0) - 0.4 * (
            (dxy.iloc[-1] - dxy_mean) / dxy_std if dxy_std else 0)

        new_gold_state = self.sd['gold_state']
        gold_reason = ""

        if self.sd['gold_state'] == 'RIGHT' and gc_price < gc_ma60:
            new_gold_state, gold_reason = 'NONE', "跌破季线，趋势终结"
        elif self.sd['gold_state'] == 'LEFT':
            if gc_price < self.sd['left_stop_price']:
                new_gold_state, gold_reason = 'NONE', "击穿深渊防线，抄底失败"
            elif gc_price > gc_ma60:
                new_gold_state, gold_reason = 'RIGHT', "左侧站上季线，加仓至右侧"

        if new_gold_state == 'NONE' and 'GOLD_EXIT' not in self.sd['daily_alerts']:
            if gc_price > gc_ma20 and gc_trend_up and macro_score > Config.MACRO_THRESHOLD and vix.iloc[
                -1] < Config.VIX_PANIC_LINE:
                new_gold_state, gold_reason = 'RIGHT', "宏观顺风+均线多头，右侧全仓"
            elif gc_price <= gc_target and gc_rsi <= 30 and macro_score > Config.MACRO_THRESHOLD:
                new_gold_state, gold_reason = 'LEFT', "砸出黄金坑，启动左侧摸底"
                self.sd['left_stop_price'] = gc_target * 0.95

        gold_weight = 0.80 if new_gold_state == 'RIGHT' else (0.40 if new_gold_state == 'LEFT' else 0.0)

        new_pool_state, pool_reason = 'CASH', "防御状态，吃逆回购利息"
        if qqq_price > qqq_ma60:
            new_pool_state, pool_reason = 'QQQ', "纳指季线上，拥抱科技"
        elif dia_price > dia_ma60:
            new_pool_state, pool_reason = 'DIA', "蓝筹季线上，拥抱红利"
        pool_weight = 1.0 - gold_weight

        logger.info(
            f"▶ 黄金: {self.sd['gold_state']} -> {new_gold_state} | 蓄水池: {self.sd['pool_state']} -> {new_pool_state}")

        if new_gold_state != self.sd['gold_state'] or new_pool_state != self.sd['pool_state']:
            alert_id = f"{new_gold_state}_{new_pool_state}"
            if alert_id not in self.sd['daily_alerts']:
                self.sd['daily_alerts'].append(alert_id)
                if new_gold_state == 'NONE' and self.sd['gold_state'] != 'NONE':
                    self.sd['daily_alerts'].append('GOLD_EXIT')

                msg = (f"【资产再平衡指令】\n\n"
                       f"1. 黄金动作: {gold_reason if gold_reason else '保持原状'}\n"
                       f"2. 蓄水池动作: {pool_reason if new_pool_state != self.sd['pool_state'] else '保持原状'}\n\n"
                       f"请立即按以下比例调仓：\n"
                       f"[黄金]: {gold_weight * 100:.0f}%\n"
                       f"[ETF蓄水池 ({new_pool_state})]: {pool_weight * 100:.0f}%")
                self.push_alert("双核调仓预警 (配置更新)", msg)

        self.sd['gold_state'] = new_gold_state
        self.sd['pool_state'] = new_pool_state
        self.state_manager.save_state()


if __name__ == "__main__":
    logger.info("🚀 GitHub Actions 定时任务启动...")
    sm = LocalStateManager()
    sniper = DualCoreSniper(sm)
    sniper.run_scan()
    logger.info("🏁 本次执行结束，容器即将销毁。")
