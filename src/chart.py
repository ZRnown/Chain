from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
import math
import random

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker
import mplfinance as mpf
import pandas as pd

from .models import TokenMetrics


def render_chart(
    metrics: TokenMetrics,
    bars: List[Dict[str, Any]],
    outfile: Optional[str | Path] = None,
) -> Optional[BytesIO]:
    """
    绘制标准的K线图（类似TradingView风格）
    """
    import logging
    logger = logging.getLogger("ca_filter_bot.chart")
    
    # 1. 数据转换
    logger.info(f"📊 Rendering chart: {len(bars)} bars provided, price: ${metrics.price_usd}")
    
    if not bars:
        error_msg = "No chart data provided - Birdeye API failed to return data"
        logger.error(f"❌ {error_msg}")
        raise ValueError(error_msg)
    
    df = _bars_to_df(bars)
    
    if df is None or df.empty:
        error_msg = "Chart data conversion failed - invalid data format"
        logger.error(f"❌ {error_msg}")
        raise ValueError(error_msg)
    
    # 调试：检查数据
    if len(df) > 0:
        sample = df.iloc[0]
        logger.debug(f"📊 Sample K-line: O={sample['Open']:.8f}, H={sample['High']:.8f}, L={sample['Low']:.8f}, C={sample['Close']:.8f}")
        # 检查是否有实体（Open != Close）
        has_body = (df['Open'] != df['Close']).any()
        logger.debug(f"📊 Has K-line bodies: {has_body}, Total bars: {len(df)}")
    
    # 截取最近60根（1小时）
    if len(df) > 60:
        df = df.iloc[-60:]
    
    # 2. 计算关键数据
    latest_close = float(df["Close"].iloc[-1])
    first_open = float(df["Open"].iloc[0])
    change_amt = latest_close - first_open
    change_pct = (change_amt / first_open * 100) if first_open != 0 else 0
    
    # 3. 颜色定义（涨绿跌红）
    COLOR_UP = "#089981"    # 涨：绿色
    COLOR_DOWN = "#F23645"  # 跌：红色
    COLOR_BG = "#0D1117"    # 背景：深色
    GRID_COLOR = "#2A2F35"  # 深灰网格
    
    is_up = change_pct >= 0
    main_color = COLOR_UP if is_up else COLOR_DOWN
    
    # 4. 创建市场颜色配置
    # 关键：确保K线实体有颜色，不是空心
    mc = mpf.make_marketcolors(
        up=COLOR_UP,      # 涨：绿色实体
        down=COLOR_DOWN,  # 跌：红色实体
        edge={'up': COLOR_UP, 'down': COLOR_DOWN},  # 边框颜色（与实体同色）
        wick={'up': COLOR_UP, 'down': COLOR_DOWN},  # 影线颜色
        volume={'up': COLOR_UP + "80", 'down': COLOR_DOWN + "80"},  # 成交量（带透明度）
        ohlc='i',  # 继承涨跌色
        alpha=1.0  # 完全不透明，确保实体可见
    )
    
    # 5. 创建样式
    style = mpf.make_mpf_style(
        base_mpf_style='nightclouds',
        marketcolors=mc,
        gridstyle=':',
        gridcolor=GRID_COLOR,
        facecolor=COLOR_BG,
        figcolor=COLOR_BG,
        rc={
            'font.family': 'DejaVu Sans',
            'font.size': 9,
            'axes.labelsize': 8,
            'axes.linewidth': 0.5,
            'axes.edgecolor': '#4B5563',
            'axes.labelcolor': '#E5E7EB',
            'xtick.color': '#E5E7EB',
            'ytick.color': '#E5E7EB',
        }
    )
    
    # 6. 确保数据列名正确（mplfinance要求首字母大写）
    # 确保列顺序正确：Open, High, Low, Close
    df_plot = df[['Open', 'High', 'Low', 'Close']].copy()
    
    # 调试：打印前几行数据
    if len(df_plot) > 0:
        logger.debug(f"📊 First 3 rows:\n{df_plot.head(3)}")
        logger.debug(f"📊 Data types: {df_plot.dtypes}")
        logger.debug(f"📊 Open != Close count: {(df_plot['Open'] != df_plot['Close']).sum()}/{len(df_plot)}")
    
    # 7. 绘制K线图（不显示成交量）
    fig, axlist = mpf.plot(
        df_plot,
        type='candle',  # 标准K线图
        volume=False,  # 不显示成交量
        style=style,
        figsize=(10, 6),
        datetime_format='%H:%M',
        xrotation=0,
        ylabel='',
        scale_width_adjustment=dict(candle=1.2),
        tight_layout=True,
        returnfig=True,
        show_nontrading=False,
        warn_too_much_data=10000
    )
    
    ax_main = axlist[0]  # K线图主图
    
    # 8. Y轴价格格式化（处理小数值）
    if latest_close > 0:
        # 计算需要的小数位数
        decimals = max(0, -int(math.floor(math.log10(latest_close))) + 4)
    else:
        decimals = 8
    
    formatter_str = f"{{:.{decimals}f}}"
    
    def price_fmt(x, p):
        return formatter_str.format(x).rstrip('0').rstrip('.')
    
    ax_main.yaxis.set_major_formatter(ticker.FuncFormatter(price_fmt))
    ax_main.yaxis.tick_right()  # 价格在右侧
    
    # 9. 左上角信息框（小尺寸，避免被蜡烛图遮挡）
    # 使用半透明背景框，确保文字清晰可见
    price_display = formatter_str.format(latest_close)
    sign = "+" if change_pct > 0 else ""
    change_str = f"{sign}{change_pct:.2f}%"
    
    # 创建信息框文本（紧凑格式，三行）
    info_lines = [
        f"{metrics.symbol} / USD",
        f"${price_display}  {change_str} (1H)",
    ]
    info_text = "\n".join(info_lines)
    
    # 绘制半透明背景框（白色背景，带边框，小尺寸）
    props = dict(
        boxstyle='round,pad=0.3',
        facecolor=COLOR_BG,
        alpha=0.88,
        edgecolor=main_color,
        linewidth=1.2,
    )
    
    # 在左上角显示（x=0.02表示左对齐，y=0.98表示顶部）
    # 小字体，紧凑布局
    ax_main.text(
        0.02, 0.98,
        info_text,
        transform=ax_main.transAxes,
        fontsize=9,  # 小字体
        fontweight='bold',
        color='#E5E7EB',
        bbox=props,
        verticalalignment='top',
        horizontalalignment='left',  # 左对齐
        family='monospace',  # 等宽字体，价格对齐更整齐
        zorder=10  # 确保在最上层，不被K线遮挡
    )
    
    # 10. 清理标题
    ax_main.set_title("")
    
    # 11. 保存到内存（BytesIO）而不是文件
    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=120, bbox_inches='tight', pad_inches=0.05, facecolor=COLOR_BG)
    buffer.seek(0)  # 重置指针到开头
    plt.close(fig)
    
    return buffer


def _bars_to_df(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    将原始K线数据转换为DataFrame
    支持 Birdeye API 格式: {t (unixTime), o, h, l, c, v}
    关键：Birdeye返回的数据已经是1分钟K线，不需要重采样
    """
    import logging
    logger = logging.getLogger("ca_filter_bot.chart")
    
    if not bars:
        logger.warning("⚠️ No bars data provided")
        return pd.DataFrame()
    
    logger.debug(f"📊 Converting {len(bars)} bars to DataFrame")
    
    # 先检查原始数据格式
    if bars:
        sample_bar = bars[0]
        logger.debug(f"📊 Sample raw bar keys: {list(sample_bar.keys())}")
        logger.debug(f"📊 Sample raw bar: {sample_bar}")
    
    df = pd.DataFrame(bars)
    
    # 字段映射
    rename_map = {
        "t": "Date", "time": "Date",
        "o": "Open", "open": "Open",
        "h": "High", "high": "High",
        "l": "Low", "low": "Low",
        "c": "Close", "close": "Close",
        "v": "Volume", "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    
    # 检查必需字段
    required = ["Date", "Open", "High", "Low", "Close"]
    if not all(col in df.columns for col in required):
        logger.error(f"❌ Missing required columns. Available: {list(df.columns)}")
        return pd.DataFrame()
    
    # 转换时间戳（Birdeye返回的是秒级时间戳 unixTime）
    df["Date"] = pd.to_numeric(df["Date"], errors='coerce')
    # 判断是秒还是毫秒：如果大于1e11就是毫秒，否则是秒
    if len(df) > 0:
        first_ts = df["Date"].iloc[0]
        if first_ts > 1e11:
            unit = 'ms'
        else:
            unit = 's'
        logger.debug(f"📊 Time unit: {unit}, first timestamp: {first_ts}")
    else:
        unit = 's'
    
    df["Date"] = pd.to_datetime(df["Date"], unit=unit, errors='coerce')
    
    # 设置索引
    df = df.set_index("Date")
    df.index = pd.DatetimeIndex(df.index)
    
    # 确保数值类型（关键：保持原始的开盘价和收盘价）
    cols = ["Open", "High", "Low", "Close"]
    df[cols] = df[cols].apply(pd.to_numeric, errors='coerce')
    
    # 移除无效数据
    before_drop = len(df)
    df = df.dropna(subset=cols)
    after_drop = len(df)
    if before_drop != after_drop:
        logger.warning(f"⚠️ Dropped {before_drop - after_drop} rows with NaN values")
    
    # 检查数据有效性
    if len(df) > 0:
        # 检查是否有实体（Open != Close）
        body_count = (df['Open'] != df['Close']).sum()
        logger.debug(f"📊 Bars with body (Open != Close): {body_count}/{len(df)}")
        
        # 检查数据范围
        logger.debug(f"📊 Price range: O[{df['Open'].min():.8f}, {df['Open'].max():.8f}], "
                    f"C[{df['Close'].min():.8f}, {df['Close'].max():.8f}]")
    
    # 重要：Birdeye返回的数据已经是1分钟K线，不需要重采样
    # 重采样会破坏原始的开盘价和收盘价
    # 只需要确保数据按时间排序
    df = df.sort_index()
    
    return df


def _generate_fallback_chart(metrics: TokenMetrics) -> pd.DataFrame:
    """
    生成模拟K线数据（当没有真实数据时）
    关键：确保Open和Close不同，才能显示K线实体
    """
    import logging
    logger = logging.getLogger("ca_filter_bot.chart")
    
    current_price = metrics.price_usd or 0.0001
    if current_price == 0:
        current_price = 0.0001
    
    logger.warning(f"⚠️ Using fallback chart data for price: {current_price}")
    
    # 生成最近60分钟的数据
    now = datetime.now()
    timestamps = [now - timedelta(minutes=i) for i in range(59, -1, -1)]
    
    # 添加随机波动，确保每根K线都有实体（Open != Close）
    # 使用固定seed（基于价格），确保同一价格生成的图表一致
    # 将价格转换为整数作为seed，确保相同价格生成相同图表
    price_int = int(current_price * 1000000000)  # 转换为整数（保留9位小数精度）
    random.seed(price_int % 1000000)  # 使用价格作为seed，确保同一价格生成相同图表
    data = []
    base_price = current_price
    
    for i, ts in enumerate(timestamps):
        # 每根K线都有不同的开盘价和收盘价
        # 使用趋势 + 随机波动
        trend = (i / len(timestamps) - 0.5) * 0.02  # 轻微趋势
        random_change = random.uniform(-0.01, 0.01)  # 随机波动
        
        # 开盘价：基于基础价格 + 趋势
        open_price = base_price * (1 + trend + random_change)
        
        # 收盘价：开盘价 + 随机变化（确保不同）
        close_change = random.uniform(-0.005, 0.005)
        close_price = open_price * (1 + close_change)
        
        # 确保收盘价和开盘价不同（至少0.1%的差异）
        if abs(close_price - open_price) / open_price < 0.001:
            close_price = open_price * (1 + (0.001 if random.random() > 0.5 else -0.001))
        
        # 最高价和最低价
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.003))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.003))
        
        data.append({
            "Date": ts,
            "Open": open_price,
            "High": high_price,
            "Low": low_price,
            "Close": close_price,
            "Volume": random.randint(500, 1500),
        })
        
        # 更新基础价格（模拟价格走势）
        base_price = close_price
    
    df = pd.DataFrame(data)
    df = df.set_index("Date")
    df.index = pd.DatetimeIndex(df.index)
    
    # 验证数据
    body_count = (df['Open'] != df['Close']).sum()
    logger.debug(f"📊 Fallback chart: {body_count}/{len(df)} bars have body (Open != Close)")
    
    return df
