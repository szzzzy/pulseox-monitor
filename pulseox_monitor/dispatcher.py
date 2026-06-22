# =============================================================================
# 消息分发器 —— 将 MQTT 上行数据解析为 FlexibleMessage。
#
# 支持两条独立的解析路径：
#   1. 主路径（JSON）：ESP32 通过 MQTT pulseox/data 发布的 JSON 对象。
#   2. 备选路径（CSV）：STM32F407 直接输出的 USART2 原始 CSV 行（M,...\\r\\n）。
#
# CSV 路径是独立的可选 parser，不会破坏 JSON 主路径。
# 两条路径最终都产出一致的 FlexibleMessage，供上游 UI 统一消费。
# =============================================================================

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .models import FlexibleMessage, MessageValidationError

# =============================================================================
# STM32F407 USART2 CSV 列映射表
#
# 当前 STM32 固件通过 USART2 输出 110 列 CSV 数据，格式为：
#   M,<col1>,<col2>,...,<col109>\\r\\n
#
# 列 0 固定为帧类型标识符 "M"（measurement）。
# 0-101 列保持旧顺序不变，新增 102-109 为 ECG 质量字段。
# 本映射表将 CSV 列索引映射为 JSON 字段名，
# 仅映射 PC 上位机需要显示和使用的列，
# OLED only / internal 字段不在此表中。
# =============================================================================

CSV_COLUMN_MAP: dict[int, str] = {
    # ---- 帧类型 ----
    0: "frame_type",

    # ---- RTC / 日期时间（列 1-3） ----
    1: "rtc_valid",
    2: "date",
    3: "time",

    # ---- PPG 原始信号（列 4-7） ----
    4: "red",
    5: "ir",
    6: "baseline_ir",
    7: "finger",

    # ---- 核心生命体征：valid/value 成对，valid 在前（列 8-15） ----
    8: "bpm_valid",
    9: "bpm",
    10: "spo2_valid",
    11: "spo2",
    12: "rr_valid",
    13: "rr",
    14: "ibi_valid",
    15: "ibi",

    # ---- HRV 时域指标（列 16-19） ----
    16: "hrv_valid",
    17: "mean_ibi",
    18: "sdnn",
    19: "rmssd",

    # ---- 运动检测（列 20-21） ----
    20: "motion_artifact",
    21: "motion_score",

    # ---- HRV Poincaré / 心律（列 22-25） ----
    22: "sd1",
    23: "sd2",
    24: "sd1_sd2_x100",
    25: "rhythm_irregular",

    # ---- HRV 频域指标（列 26-29） ----
    26: "hrv_freq_valid",
    27: "lf_power_x100",
    28: "hf_power_x100",
    29: "lf_hf_x100",

    # ---- 信号质量 / 灌注指数 / 平衡状态（列 30-38） ----
    30: "signal_quality",
    31: "raw_signal_present",
    32: "signal_ir_pi_x1000",
    33: "signal_red_pi_x1000",
    34: "signal_ir_ac_rms",
    35: "signal_red_ac_rms",
    36: "spo2_ratio_valid",
    37: "spo2_ratio_x1000",
    38: "spo2_balance_status",

    # ---- PPG 信号诊断 / 手指检测统计（列 39-46） ----
    39: "baseline_range_ir",
    40: "adaptive_finger_on_delta",
    41: "adaptive_finger_off_delta",
    42: "ir_signal_delta",
    43: "ir_signal_span",
    44: "red_signal_span",
    45: "finger_on_confirm_count",
    46: "finger_off_confirm_count",

    # ---- 传感器诊断（列 47-60） ----
    47: "sensor_last_read_status",
    48: "sensor_error_streak",
    49: "sensor_fifo_write_ptr",
    50: "sensor_fifo_read_ptr",
    51: "sensor_fifo_overflow_count",
    52: "sensor_fifo_available_samples",
    53: "sensor_read_ok_count",
    54: "sensor_read_busy_count",
    55: "sensor_read_error_count",
    56: "sensor_recover_count",
    57: "sensor_last_sample_tick",
    58: "sensor_sample_change_count",
    59: "sensor_sample_same_count",
    60: "sensor_last_i2c_error",

    # ---- RTC / UART（列 61-63） ----
    61: "rtc_read_ok",
    62: "uart_rx_message_valid",
    63: "uart_tx_message_valid",

    # ---- SD / Display / Debug（列 64-71） ----
    64: "sd_log_active",
    65: "sd_state",
    66: "sd_error",
    67: "sd_total_written",
    68: "display_refresh_count",
    69: "display_last_refresh_tick",
    70: "debug_mode",
    71: "current_page",

    # ---- ECG（列 72-77） ----
    72: "ecg_valid",
    73: "ecg_hr",
    74: "ecg_rr_ms",
    75: "ecg_lead_off",
    76: "ecg_r_peak_ms",
    77: "ecg_filtered",

    # ---- PTT（列 78-79） ----
    78: "ptt_valid",
    79: "ptt_ms",

    # ---- ECG 计数器（列 80-84） ----
    80: "ecg_sample_count",
    81: "ecg_adc_sat_count",
    82: "ecg_dma_overflow_count",
    83: "ecg_lead_off_count",
    84: "ecg_no_r_peak_timeout_count",

    # ---- 崩溃（列 85-91） ----
    85: "crash_flag",
    86: "crash_source",
    87: "crash_task",
    88: "crash_phase",
    89: "crash_tick",
    90: "reboot_count",
    91: "reset_flags",

    # ---- 任务阶段（列 92-95） ----
    92: "max_task_phase",
    93: "ui_task_phase",
    94: "sd_task_phase",
    95: "wdt_task_phase",

    # ---- 任务栈高水位（列 96-99） ----
    96: "max_task_stack_hwm",
    97: "ui_task_stack_hwm",
    98: "sd_task_stack_hwm",
    99: "wdt_task_stack_hwm",

    # ---- 任务心跳（列 100-101） ----
    100: "max_task_heartbeat",
    101: "ui_task_heartbeat",

    # ---- ECG 质量字段（列 102-109，v3 新增） ----
    102: "ecg_signal_quality",
    103: "ecg_invalid_reason",
    104: "ecg_raw_span",
    105: "ecg_filtered_span",
    106: "ecg_noise_level",
    107: "ecg_qrs_threshold",
    108: "ecg_peak_snr_x100",
    109: "ecg_dma_available_high_watermark",
}

# STM32 CSV 帧的前缀标识 —— 所有原始 CSV 帧必须以 "M," 开头
_STM32_CSV_PREFIX = "M,"

# 当前 STM32 固件预期的 CSV 列数（110 列）
_EXPECTED_CSV_COLUMNS = 110


# =============================================================================
# _parse_stm32_csv_line —— STM32 USART2 原始 CSV → dict
# =============================================================================

def _parse_stm32_csv_line(line: str) -> dict[str, Any] | None:
    """将 STM32 USART2 原始 CSV 行转换为 dict，供 FlexibleMessage.from_dict 消费。

    解析策略：
      1. 去除首尾空白和 \\r\\n 换行符。
      2. 按逗号拆分为字段列表。
      3. 校验第一列是否为 "M"（帧类型标识）。
      4. 若列数不等于 110，记录到 parse_warnings，但不丢弃数据。
      5. 按 CSV_COLUMN_MAP 将已知列映射为 JSON 字段，
         自动推断 int/float 类型。
      6. 空字符串和 "--" 视为缺失值，跳过不写入。
      7. 未知列（非空且不在映射表中）收集到 extra_fields。

    参数：
      line: STM32 USART2 原始 CSV 行字符串。

    返回：
      成功时返回 dict，可直接传入 FlexibleMessage.from_dict()；
      失败时返回 None（调用方应触发 MessageValidationError）。
    """
    # ---- 步骤 1: 清洗输入 ----
    stripped = line.strip().rstrip("\r\n")

    # ---- 步骤 2: 按逗号拆分 ----
    parts = [p.strip() for p in stripped.split(",")]
    if len(parts) < 2:
        # 至少需要帧类型 "M" + 一个字段
        return None

    # ---- 步骤 3: 校验帧类型 ----
    if parts[0] != "M":
        return None

    # ---- 步骤 4: 构建基础 payload ----
    payload: dict[str, Any] = {
        "message": "measurement",
        "protocol": "csv",
        "raw_line": line.strip(),        # 保留原始行，供 Raw Frame 视图展示
        "field_count": len(parts),       # 实际列数
        "parse_warnings": [],
        "extra_fields": [],
    }

    # 列数不匹配时记录警告，但不丢弃整帧
    if len(parts) != _EXPECTED_CSV_COLUMNS:
        payload["parse_warnings"].append(
            f"CSV columns={len(parts)}, expected={_EXPECTED_CSV_COLUMNS}"
        )

    # ---- 步骤 5: 映射已知列 ----
    for idx, field_name in CSV_COLUMN_MAP.items():
        if idx >= len(parts):
            # 列数不足，跳过超出范围的映射
            break
        raw_val = parts[idx]
        # 空字符串和 "--" 是 STM32 的缺失值哨兵，跳过
        if raw_val == "" or raw_val == "--":
            continue
        # 自动类型推断：含小数点或科学计数法的解析为 float，否则为 int
        try:
            if "." in raw_val or "e" in raw_val.lower():
                payload[field_name] = float(raw_val)
            else:
                payload[field_name] = int(raw_val)
        except ValueError:
            # 无法解析为数字的保留原始字符串
            payload[field_name] = raw_val

    # ---- 步骤 6: 收集未知非空字段到 extra_fields ----
    for idx, val in enumerate(parts):
        if idx not in CSV_COLUMN_MAP and val.strip() not in ("", "--"):
            payload["extra_fields"].append(f"col{idx}={val}")

    # ---- 步骤 7: PC 侧判定 —— 列数匹配且无警告时视为解析成功 ----
    # parse_ok / rx_ms 不是 STM32 CSV 列，仅由 PC 侧解析器生成。
    payload["parse_ok"] = (
        len(parts) == _EXPECTED_CSV_COLUMNS and len(payload["parse_warnings"]) == 0
    )

    return payload


# =============================================================================
# MessageDispatcher —— 消息分发器
# =============================================================================

# 可用于推断消息为 measurement 的测量字段集合
_MEASUREMENT_HINT_KEYS: set[str] = {
    "bpm", "spo2", "red", "ir", "rr", "ibi",
    "signal_quality", "ecg_hr", "ptt_ms", "modules",
    "finger", "ecg_filtered", "ecg_raw", "sdnn", "rmssd",
    "motion_artifact", "motion_score",
}

# 可用于推断消息类型的 type-like 字段
_TYPE_LIKE_KEYS: set[str] = {"type", "msg", "message_type", "msg_type"}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "bpm": (
        "heart_rate", "heartRate", "hr", "HR", "pulse", "pulse_rate",
        "pulseRate",
    ),
    "bpm_valid": (
        "bpmValid", "heart_rate_valid", "heartRateValid", "hr_valid",
        "hrValid", "pulse_valid", "pulseValid",
    ),
    "spo2": (
        "SpO2", "SPO2", "spo2_pct", "spo2Percent", "oxygen",
        "blood_oxygen", "bloodOxygen",
    ),
    "spo2_valid": (
        "spo2Valid", "SpO2_valid", "SpO2Valid", "oxygen_valid",
        "oxygenValid",
    ),
    "rr": (
        "resp_rate", "respRate", "respiratory_rate", "respiratoryRate",
        "breathing_rate", "breathingRate",
    ),
    "rr_valid": (
        "rrValid", "resp_rate_valid", "respRateValid",
        "respiratory_rate_valid", "respiratoryRateValid",
    ),
    "ibi": ("ibi_ms", "ibiMs", "interval_ms", "intervalMs"),
    "ibi_valid": ("ibiValid", "ibi_ms_valid", "ibiMsValid"),
    "red": ("red_raw", "redRaw", "ppg_red", "ppgRed", "red_adc", "redAdc"),
    "ir": ("ir_raw", "irRaw", "ppg_ir", "ppgIr", "ir_adc", "irAdc"),
    "baseline_ir": (
        "ir_baseline", "irBaseline", "baselineIR", "baseline", "dc_ir",
        "dcIr",
    ),
    "signal_quality": (
        "signalQuality", "signal_quality_score", "signalQualityScore",
        "sq", "SQ", "quality",
    ),
    "motion_score": ("motionScore",),
    "motion_artifact": ("motionArtifact", "motion_detected", "motionDetected"),
    "signal_ir_pi_x1000": ("ir_pi_x1000", "irPiX1000"),
    "signal_red_pi_x1000": ("red_pi_x1000", "redPiX1000"),
    "signal_ir_ac_rms": ("ir_ac_rms", "irAcRms", "ir_ac", "irAC"),
    "signal_red_ac_rms": ("red_ac_rms", "redAcRms", "red_ac", "redAC"),
    "spo2_ratio_x1000": ("ratio_x1000", "ratioX1000", "r_ratio", "rRatio"),
    "spo2_ratio_valid": ("ratio_valid", "ratioValid", "r_ratio_valid", "rRatioValid"),
    "ir_signal_delta": ("ir_delta", "irDelta"),
    "ir_signal_span": ("ir_span", "irSpan"),
    "red_signal_span": ("red_span", "redSpan"),
    "mean_ibi": ("mean_ibi_ms", "meanIbi", "meanIbiMs"),
    "sd1_sd2_x100": ("sd1_sd2", "sd1Sd2", "sd1Sd2X100"),
    "lf_power_x100": ("lf_power", "lfPower", "lfPowerX100"),
    "hf_power_x100": ("hf_power", "hfPower", "hfPowerX100"),
    "lf_hf_x100": ("lf_hf", "lfHf", "lfHfX100"),
    "ecg_filtered": ("ecg_filt", "ecgFilt", "ecgFiltered"),
    "ecg_raw": ("ecgRaw",),
    "ecg_hr": ("ecgHeartRate", "ecg_heart_rate", "ecgHr"),
    "ecg_valid": ("ecgValid",),
    "ecg_rr_ms": ("ecg_rr", "ecgRr", "ecgRrMs"),
    "ptt_ms": ("ptt", "pttMs"),
    "ptt_valid": ("pttValid",),
    "ecg_signal_quality": ("ecgSignalQuality", "ecg_sq", "ecgSq"),
    "ecg_invalid_reason": ("ecgInvalidReason",),
    "ecg_raw_span": ("ecgRawSpan",),
    "ecg_filtered_span": ("ecgFilteredSpan", "ecgFiltSpan"),
    "ecg_noise_level": ("ecgNoiseLevel",),
    "ecg_qrs_threshold": ("ecgQrsThreshold",),
    "ecg_peak_snr_x100": ("ecgPeakSnrX100", "ecgPeakSNRx100"),
    "ecg_dma_available_high_watermark": ("ecgDmaAvailHwm", "ecg_dma_avail_hwm"),
}


def _scalar_value(value: Any) -> Any:
    """取出常见包装结构中的测量标量，保留原值类型。"""
    if isinstance(value, dict):
        for key in ("value", "val", "last", "avg", "mean", "filtered", "raw"):
            if key in value:
                return _scalar_value(value[key])
        return value
    if isinstance(value, list):
        for item in reversed(value):
            scalar = _scalar_value(item)
            if not isinstance(scalar, (dict, list)):
                return scalar
        return value
    return value


def _normalize_field_aliases(payload: dict[str, Any]) -> None:
    """把常见别名字段补齐到标准顶层字段，不覆盖已有标准字段。"""
    for canonical, aliases in _FIELD_ALIASES.items():
        if payload.get(canonical) is not None:
            continue
        for alias in aliases:
            if alias in payload and payload.get(alias) is not None:
                payload[canonical] = _scalar_value(payload[alias])
                break


def _normalize_payload(payload: dict) -> dict:
    """对 JSON 解析后的 payload 做归一化处理，返回新 dict。

    归一化步骤（按顺序）：
      1. data / payload / body / value 子对象展平到顶层。
         - 若值为 dict，展平其键值（外层键优先，不覆盖）。
         - 若值为 JSON 字符串，尝试解析为 dict 后展平。
      2. modules 展平：modules.bpm.value → bpm, modules.bpm.valid → bpm_valid。
         - 同样处理 spo2, rr, ibi, ecg_hr, ptt_ms 等模块。
         - 不覆盖已有顶层字段。
      3. 缺失 message 时推断：
         - type/msg/message_type 等字段值为 measurement/data/telemetry → "measurement"。
         - payload 包含测量字段（bpm/spo2/red/ir 等）→ "measurement"。
    """
    # 步骤 1: 展平嵌套子对象（data / payload / body / value）
    result: dict = {}
    nested_sub_keys = ("data", "payload", "body", "value")
    for key in nested_sub_keys:
        nested = payload.get(key)
        flattened: dict | None = None
        if isinstance(nested, dict):
            flattened = nested
        elif isinstance(nested, str):
            try:
                inner = json.loads(nested)
                if isinstance(inner, dict):
                    flattened = inner
            except (json.JSONDecodeError, TypeError):
                pass
        if flattened is not None:
            # 原有顶层键优先，不被子对象覆盖
            result = {**flattened, **payload, **result}
    # 合并原始 payload（子对象值在上，原始值在下，保证不覆盖）
    result = {**payload, **result}
    # 移除已被展平处理的嵌套键本身（保留原始子对象）
    # 注意：如果 key 对应的值已被展平，原始嵌套对象仍在 result 中
    # 此处不删除，因为某些代码可能依赖 raw 中的嵌套结构

    # 步骤 2: 展平 modules
    modules = result.get("modules")
    if isinstance(modules, dict):
        for mod_name, mod_data in modules.items():
            if not isinstance(mod_data, dict):
                continue
            # value → <module_name>
            if "value" in mod_data:
                mod_value = _scalar_value(mod_data["value"])
                if result.get(mod_name) is None:
                    result[mod_name] = mod_value
            # valid → <module_name>_valid
            valid_key = f"{mod_name}_valid"
            if "valid" in mod_data:
                mod_valid = mod_data["valid"]
                if result.get(valid_key) is None:
                    result[valid_key] = mod_valid

    # 步骤 2b: 常见别名字段补齐为标准字段
    _normalize_field_aliases(result)

    # ESP32 parse_error 早期固件有时把原始 STM32 行放在 raw。
    # GUI 诊断页统一读 raw_line，保留 raw 本身不删除。
    if result.get("message") == "parse_error" and result.get("raw_line") is None:
        raw_value = result.get("raw")
        if isinstance(raw_value, str):
            result["raw_line"] = raw_value

    # 步骤 3: 推断缺失的 message 字段
    if not result.get("message"):
        # 3a: 检查 type-like 字段
        for f in _TYPE_LIKE_KEYS:
            v = result.get(f)
            if isinstance(v, str) and v.lower() in ("measurement", "data", "telemetry"):
                result["message"] = "measurement"
                break

        # 3b: 检查测量字段
        if not result.get("message"):
            if _MEASUREMENT_HINT_KEYS & set(result.keys()):
                result["message"] = "measurement"

    return result


class MessageDispatcher:
    """消息分发器：将 MQTT 上行文本解析为 FlexibleMessage。

    对外唯一入口是 dispatch() 方法。
    内部根据内容前缀自动选择 JSON 或 CSV 解析路径。
    """

    def dispatch(
        self,
        payload_text: str | bytes,
        received_at: datetime | None = None,
    ) -> FlexibleMessage | None:
        """解析 MQTT 上行文本为 FlexibleMessage。

        解析流程：
          1. 若输入为 bytes，先解码为 UTF-8 字符串。
          2. 若文本以 "M," 开头 → 走 CSV 备选路径。
          3. 否则 → 走 JSON 主路径。
          4. JSON 路径还处理嵌套 data 子对象的展平。

        参数：
          payload_text: MQTT 上行负载（UTF-8 字符串或 bytes）。
          received_at:   PC 端接收时间，None 时使用当前时间。

        返回：
          成功时返回 FlexibleMessage 实例。
          返回 None 表示不可恢复的解析失败。

        异常：
          MessageValidationError —— 输入不是合法的 UTF-8、JSON、CSV 时抛出。
        """
        # ---- 步骤 1: bytes → str ----
        if isinstance(payload_text, bytes):
            try:
                payload_text = payload_text.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MessageValidationError("MQTT 负载不是有效的 UTF-8 文本") from exc

        # ---- 步骤 2: 备选路径 —— STM32 原始 CSV ----
        if payload_text.startswith(_STM32_CSV_PREFIX):
            csv_payload = _parse_stm32_csv_line(payload_text)
            if csv_payload is not None:
                return FlexibleMessage.from_dict(csv_payload, received_at)
            # CSV 解析失败（格式不正确）
            raise MessageValidationError("CSV 格式不正确，无法解析")

        # ---- 步骤 3: 主路径 —— ESP32 JSON ----
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise MessageValidationError("MQTT 上行负载不是合法 JSON") from exc

        # JSON 必须是对象（dict），不接受数组或标量
        if not isinstance(payload, dict):
            raise MessageValidationError("MQTT 上行 JSON 必须是对象")

        # ---- 步骤 4: payload 归一化 ----
        payload = _normalize_payload(payload)

        return FlexibleMessage.from_dict(payload, received_at)
