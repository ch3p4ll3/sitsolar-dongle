"""Read-only register map of the FuturaPulse hybrid inverter.

Transcribed from *FuturaPulse hybrid inverter modbus protocol v0.0.7*.
Only the read-only input registers (30000~39999, function code 0x04) are
listed here; the writable holding registers (40001~49999, FC 0x03/0x06/0x10)
are deliberately omitted because this project never writes.

Register addresses in the document are used **verbatim** as the Modbus PDU
address -- no 30000 offset needs to be subtracted.  That is confirmed by the
document's own example frames (``01 04 75 94 ...`` reads register 30100) and
by live traffic from a Si-Dongle-L.

Types:

``U16``/``S16``
    One register, unsigned / two's-complement signed.
``U32``/``S32``
    Two registers, high word first.
``ASCII``
    ``length`` registers, two characters each, NUL padded.
``BITFIELD``
    One register whose bits carry independent flags.

``scale`` converts the raw integer into ``unit``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Register", "REGISTERS", "by_section", "blocks", "SECTION_ORDER"]

U16, S16, U32, S32, ASCII, BITFIELD = "U16", "S16", "U32", "S32", "ASCII", "BITFIELD"


@dataclass(frozen=True)
class Register:
    """One logical value, possibly spanning several 16-bit registers."""

    address: int
    key: str
    name: str
    dtype: str = U16
    unit: str = ""
    scale: float = 1.0
    length: int = 1
    notes: str = ""
    enum: dict | None = None
    bits: dict | None = None

    @property
    def end(self) -> int:
        """Last register address, inclusive."""
        return self.address + self.length - 1

    @property
    def span(self) -> str:
        if self.length == 1:
            return str(self.address)
        return "%d~%d" % (self.address, self.end)

    @property
    def section(self) -> str:
        """The chapter of the protocol document this register came from."""
        for lo, hi, name in SECTION_RANGES:
            if lo <= self.address <= hi:
                return name
        return ""


#: (first address, last address, document section) in document order.
SECTION_RANGES = (
    (30000, 30099, "device_info"),
    (30100, 30199, "operation_data"),
    (30200, 30299, "error_info"),
    (30300, 30399, "temperatures"),
    (30400, 30499, "ARM_data"),
    (30500, 30999, "DC_side"),
    (31000, 31999, "AC_side"),
    (32000, 32039, "EPS_parameters"),
    (32040, 32239, "battery_parameters"),
    (32240, 32299, "battery2_parameters"),
    (33000, 33099, "meter_parameters"),
    (33100, 33199, "meter2_parameters"),
    (34000, 34999, "generation_info"),
    (35000, 35999, "Italy_self-test"),
)


def _r(address, key, name, **kw) -> Register:
    kw.setdefault("length", 2 if kw.get("dtype") in (U32, S32) else 1)
    return Register(address=address, key=key, name=name, **kw)


DEVICE_TYPE = {
    0x11: "single phase",
    0x13: "three phase",
    0x21: "single phase high voltage",
    0x23: "three phase high voltage",
    0x31: "single phase low voltage",
    0x33: "three phase low voltage",
}

OPERATION_STATUS = {
    0: "waiting", 1: "self check", 2: "normal", 3: "flash",
    4: "error", 5: "debug", 7: "off-grid",
}

# v0.0.7 (change D006, 2025-03-11 "Modify 30103 describe") reduced this from
# the five values of v0.0.3 (0: no input, 1: single string connected, 2: two
# strings parallel, 3: different panels parallel, 4: same panels parallel).
MPPT_INPUT_TYPE = {
    0: "independent input", 1: "same panels parallel",
}

BATTERY_STATUS = {
    0: "sleep", 1: "charge", 2: "discharge", 3: "idle",
}

BMS_STATUS = {0: "fail", 1: "ok", 2: "battery not chosen"}

# v0.0.7 section 5.3.0 (added by change D007, 2025-06-11): 33049 reports the
# outcome of the meter self test.  0xAA55 means "still testing" and is the only
# non-sequential value in the list.
METER_TEST_RESULT = {
    0xAA55: "testing",
    1: "meter communications fail",
    2: "battery communications fail",
    3: "meter L-N reversed (single phase meter)",
    4: "meter check successful",
    5: "meter disabled",
    6: "load fluctuation too large to judge",
    7: "inverter not working normally",
    8: "other fail",
    9: "CT not connected or meter cover open",
    16: "current lower than required",
    17: "incorrect meter wiring (three-phase four-wire)",
    18: "meter and inverter interaction failed",
    19: "meter self-test failed",
}

# Appendix 1, Charts 8-10: registers 30207-30210 are four bit-field warning
# words.  Chart 11 (register 30211) is NOT a bit field -- it carries a single
# decimal warning code that addresses the *combined* space of those four words
# as `201 + 16*word_index + bit_index`.  That formula reproduces every row of
# Chart 11 exactly (201/202/204/205.../213 from word 0, 219/223/224/225/229/230
# from word 1, 241/245/247 from word 2), which is what proves 30211 is a code
# rather than a bit mask -- and proves that the value 200 sits one below the
# first possible code, i.e. "no warning".
#
# Chart 8 -> 30207
WARNING_BITS_1 = {
    0: "AC over current",
    1: "AC wave-filter and current-limiting calculation",
    2: "HVRT",
    3: "LVRT",
    4: "PV~ground voltage high",
    5: "Islanding",
    6: "DSP EEPROM error",
    7: "Clock error",
    8: "MCU_Register error",
    9: "MCU_RAM error",
    10: "MCU_ROM error",
    11: "DSP FLASH error",
    12: "ARM FLASH error",
    13: "Low temperature",
}

# Chart 9 -> 30208
WARNING_BITS_2 = {
    2: "MPPT over current",
    3: "MPPT wave-filter and current-limiting calculation",
    4: "MPPT startup voltage high",
    5: "PV abnormal (mismatch, shading)",
    6: "Boost1 wave-filter and current-limiting calculation",
    7: "Boost2 wave-filter and current-limiting calculation",
    8: "Boost3 wave-filter and current-limiting calculation",
    12: "AC wave-filter and current-limiting warning",
    13: "Conversion PWM error",
    14: "EPS overloading",
}

# Chart 10 -> 30209
WARNING_BITS_3 = {
    2: "MPPT2 over current",
    3: "MPPT3 over current",
    5: "PV voltage low",
    6: "BUS voltage low",
    8: "BuckBoost hardware overcurrent",
    9: "BuckBoost wave-filter and current-limiting",
    10: "MPPT2 wave-filter and current-limiting",
    11: "MPPT3 wave-filter and current-limiting",
    12: "Buck Boost software overcurrent",
    13: "Balance bridge software overcurrent",
    14: "Balance bridge hardware overcurrent",
}

# Chart 11 -> 30211, decoded warning codes.  Entries marked (*) are absent from
# Chart 11 in the PDF but are forced by the formula above (203 = HVRT and
# 214 = Low temperature are both present in Chart 8).
WARNING_CODES = {
    200: "no warning",
    201: "AC over current",
    202: "AC wave-filter and current-limiting calculation",
    203: "HVRT (*)",
    204: "LVRT",
    205: "PV~ground voltage high",
    206: "Islanding",
    207: "DSP EEPROM error",
    208: "Clock error",
    209: "MCU_Register error",
    210: "MCU_RAM error",
    211: "MCU_ROM error",
    212: "DSP FLASH error",
    213: "ARM FLASH error",
    214: "Low temperature (*)",
    219: "MPPT over current",
    220: "MPPT hardware over current",
    223: "Boost 1 wave-filter and current-limiting calculation",
    224: "Boost 2 wave-filter and current-limiting calculation",
    225: "Boost 3 wave-filter and current-limiting calculation",
    229: "AC wave-filter and current-limiting calculation",
    230: "PWM error",
    241: "BuckBoost hardware over current",
    245: "BuckBoost overcurrent",
    247: "Balance bridge hardware over current",
}


REGISTERS: tuple[Register, ...] = (
    # ---- device_info ----------------------------------------------
    _r(30000, "device_type", "Device type", enum=DEVICE_TYPE),
    _r(30001, "serial_number", "Serial number", dtype=ASCII, length=10),
    _r(30011, "model", "Inverter model", dtype=ASCII, length=9),
    _r(30020, "brand", "Brand info", dtype=ASCII, length=6),
    _r(30026, "manufacturer", "Manufacturer", dtype=ASCII, length=8),
    _r(30034, "inverter_version", "Inverter version"),
    _r(30041, "dsp1_version", "DSP1 version", dtype=ASCII, length=5),
    _r(30046, "dsp2_version", "DSP2 version", dtype=ASCII, length=5),
    _r(30061, "dsp1_bootloader", "DSP1 bootloader version"),
    _r(30062, "dsp2_bootloader", "DSP2 bootloader version"),
    _r(30070, "dsp_protocol_version", "DSP protocol version", dtype=ASCII, length=5),
    _r(30075, "safety_code_version", "Safety code version", dtype=ASCII, length=5),
    _r(30080, "modbus_id", "Modbus ID"),
    _r(30081, "rated_power", "Rated power", unit="W", dtype=U32),

    # ---- operation_data -----------------------------------------
    _r(30100, "operation_status", "Operation status", enum=OPERATION_STATUS),
    _r(30101, "auto_start_remaining", "Auto start time remaining", unit="s"),
    _r(30103, "mppt_input_type", "MPPT input type", enum=MPPT_INPUT_TYPE,
       notes="v0.0.3 listed five values (no input / single string connected / "
             "two strings parallel / different panels parallel / same panels "
             "parallel); v0.0.7 reduced them to two"),
    _r(30104, "mppt_string_number", "MPPT string number"),
    _r(30105, "pv_string_number", "PV string number"),
    _r(30106, "dc_power_flags", "DC power lose or updating signs", dtype=BITFIELD,
       bits={0: "master DSP updating slave DSP", 4: "DC power off"}),
    _r(30108, "safety_code_changed", "Safety code change flag bit",
       enum={0: "not changed", 1: "changed"}),
    _r(30109, "dsp_ac_power_off", "DSP AC power off",
       enum={0: "AC power on", 1: "AC power off"}),

    # ---- error_info ---------------------------------------------
    _r(30200, "dsp_error1", "DSP error 1"),
    _r(30201, "dsp_error2", "DSP error 2"),
    _r(30202, "dsp_error3", "DSP error 3"),
    _r(30203, "dsp_error4", "DSP error 4"),
    _r(30204, "dsp_error5", "DSP error 5"),
    _r(30205, "dsp_error6", "DSP error 6"),
    _r(30206, "dsp_error7", "DSP error 7"),
    _r(30207, "dsp_warning1", "DSP warning 1", dtype=BITFIELD,
       bits=WARNING_BITS_1),
    _r(30208, "dsp_warning2", "DSP warning 2", dtype=BITFIELD,
       bits=WARNING_BITS_2),
    _r(30209, "dsp_warning3", "DSP warning 3", dtype=BITFIELD,
       bits=WARNING_BITS_3),
    _r(30210, "dsp_warning4", "DSP warning 4", dtype=BITFIELD,
       notes="the protocol document gives no chart for 30210, so its bits "
             "are unknown; codes 249+ would belong to this word"),
    _r(30211, "dsp_warning5", "DSP warning 5", enum=WARNING_CODES,
       notes="Chart 11: carries a decimal warning code, not a bit mask. "
             "200 means no warning"),

    # ---- temperatures -------------------------------------------
    _r(30301, "cabin_temperature", "Cabin temperature", dtype=S16,
       unit="°C", scale=0.1),
    _r(30303, "boost_module1_temperature", "Boost module 1 temperature",
       dtype=S16, unit="°C", scale=0.1),
    _r(30305, "buckboost_temperature", "Buckboost temperature",
       dtype=S16, unit="°C", scale=0.1),
    _r(30307, "dc_ac_module_temperature", "DC/AC module temperature",
       dtype=S16, unit="°C", scale=0.1),
    _r(30316, "heat_sink_temperature", "Heat sink temperature", dtype=S16,
       unit="°C", scale=0.1),

    # ---- ARM_data ------------------------------------------------
    _r(30400, "arm_protocol_version", "ARM protocol version", dtype=ASCII, length=5),
    _r(30405, "arm_software_code_id", "ARM software code ID", dtype=ASCII, length=2),
    _r(30407, "arm_software_version", "ARM software version", dtype=ASCII, length=5),
    _r(30412, "bms_software_version", "BMS software version", dtype=ASCII, length=5,
       notes="depends on battery"),
    _r(30417, "arm_bootloader_version", "ARM bootloader version"),
    _r(30419, "arm_error", "ARM error code"),
    _r(30420, "arm_bit_errors1", "ARM bit errors 1", dtype=BITFIELD),
    _r(30421, "arm_bit_errors2", "ARM bit errors 2", dtype=BITFIELD),

    # ---- DC_side -------------------------------------------------
    _r(30500, "total_pv_power", "Total PV power", unit="kW", scale=0.01),
    _r(30501, "pv1_voltage", "PV 1 voltage", unit="V", scale=0.1),
    _r(30502, "pv1_current", "PV 1 current", unit="A", scale=0.1),
    _r(30503, "pv1_power", "PV 1 power", unit="kW", scale=0.01),
    _r(30504, "pv1_energy_today", "E-Today (PV 1)", unit="kWh", scale=0.1),
    _r(30505, "pv2_voltage", "PV 2 voltage", unit="V", scale=0.1),
    _r(30506, "pv2_current", "PV 2 current", unit="A", scale=0.1),
    _r(30507, "pv2_power", "PV 2 power", unit="kW", scale=0.01),
    _r(30508, "pv2_energy_today", "E-Today (PV 2)", unit="kWh", scale=0.1),

    # ---- AC_side -------------------------------------------------
    _r(31000, "phase_r_voltage", "R-phase voltage", unit="V", scale=0.1),
    _r(31001, "phase_r_current", "R-phase current", unit="A", scale=0.01),
    _r(31002, "grid_frequency", "Grid frequency", dtype=U32, unit="Hz", scale=0.001),
    _r(31004, "phase_r_power", "R-phase power", dtype=S32, unit="W"),
    _r(31006, "phase_s_voltage", "S-phase voltage", unit="V", scale=0.1),
    _r(31007, "phase_s_current", "S-phase current", unit="A", scale=0.01),
    _r(31010, "phase_s_power", "S-phase power", dtype=S32, unit="W"),
    _r(31012, "phase_t_voltage", "T-phase voltage", unit="V", scale=0.1),
    _r(31013, "phase_t_current", "T-phase current", unit="A", scale=0.01),
    _r(31016, "phase_t_power", "T-phase power", dtype=S32, unit="W",
       notes="the document's table says S16 but gives the range 31016~31017; "
             "the R and S phases are S32 at the same offset, so treat as S32"),
    _r(31018, "inverter_p", "P_Inv", dtype=S32, unit="W"),
    _r(31020, "inverter_q", "Q_Inv", dtype=S32, unit="var"),
    _r(31022, "power_factor", "Power factor", dtype=S16, scale=0.001,
       notes="reads a near-constant 0.999 and equals P/(31039~31040) to within "
             "0.005, but contradicts P/sqrt(P^2+Q^2) ~= 0.89 measured on the "
             "same poll. The vendor's own cloud publishes the same 0.999, so "
             "this is a firmware/setpoint value, not the instantaneous PF "
             "(the spec has a separate writable 46056 Power factors adjust)"),
    _r(31035, "grid_line_rs", "RS grid line voltage", unit="V", scale=0.1),
    _r(31036, "grid_line_st", "ST grid line voltage", unit="V", scale=0.1),
    _r(31037, "grid_line_tr", "TR grid line voltage", unit="V", scale=0.1),
    _r(31038, "grid_n_pe_voltage", "Grid N-PE voltage", dtype=S16,
       unit="V", scale=0.1),
    _r(31039, "apparent_power", "Inverter output apparent power",
       unit="VA", dtype=U32,
       notes="the spec calls this apparent power, but it tracks P (31018~31019) "
             "within 1-2 W: 419 VA vs 417 W, 425 VA vs 424 W, i.e. it is "
             "P/power_factor. True apparent power is V*I and sqrt(P^2+Q^2), "
             "which agree with each other at ~470 VA -- about 50 VA higher. Do "
             "not use as S for a VA sensor; compute sqrt(P^2+Q^2) instead"),

    # ---- EPS_parameters -----------------------------------------
    _r(32001, "eps_voltage_r", "EPS voltage R", unit="V", scale=0.1),
    _r(32002, "eps_current_r", "EPS current R", unit="A", scale=0.1),
    _r(32005, "eps_p_r", "EPS P_R", dtype=S32, unit="W"),
    _r(32007, "eps_voltage_s", "EPS voltage S", unit="V", scale=0.1),
    _r(32008, "eps_current_s", "EPS current S", unit="A", scale=0.1),
    _r(32011, "eps_p_s", "EPS P_S", dtype=S32, unit="W"),
    _r(32013, "eps_voltage_t", "EPS voltage T", unit="V", scale=0.1),
    _r(32014, "eps_current_t", "EPS current T", unit="A", scale=0.1),
    _r(32017, "eps_p_t", "EPS P_T", dtype=S32, unit="W"),
    _r(32019, "eps_total_p", "EPS total P", dtype=S32, unit="W"),
    _r(32021, "eps_total_q", "EPS total Q", dtype=S32, unit="var"),
    _r(32023, "eps_voltage_rs", "EPS Voltage RS", unit="V", scale=0.1),
    _r(32024, "eps_voltage_st", "EPS Voltage ST", unit="V", scale=0.1),
    _r(32025, "eps_voltage_tr", "EPS Voltage TR", unit="V", scale=0.1),
    _r(32026, "eps_voltage_n_pe", "EPS Voltage N_PE", unit="V", scale=0.1),

    # ---- battery_parameters -------------------------------------
    _r(32040, "battery_status", "Battery status", enum=BATTERY_STATUS),
    _r(32051, "bms_status", "BMS communication status", enum=BMS_STATUS),
    _r(32055, "battery_voltage", "Battery voltage", unit="V", scale=0.1),
    _r(32056, "battery_current", "Battery current", dtype=S16,
       unit="A", scale=0.01, notes="positive = discharge"),
    _r(32057, "battery_power", "Battery power", dtype=S32, unit="W",
       notes="positive = discharge"),
    _r(32063, "battery_temperature", "Battery temperature", dtype=S16,
       unit="°C", scale=0.1),
    _r(32064, "battery_soc", "Battery SOC", unit="%"),
    _r(32065, "battery_soh", "Battery SOH", unit="%"),
    _r(32069, "forbid_charge", "Forbid charge flag",
       enum={0: "chargeable", 1: "charge forbidden"}),
    _r(32070, "forbid_discharge", "Forbid discharge flag",
       enum={0: "dischargeable", 1: "discharge forbidden"}),
    _r(32071, "forced_mode_request", "Battery request forced charge or discharge",
       enum={0: "neither", 1: "normal charge", 2: "forced charge"}),
    _r(32072, "battery_error1", "Battery error 1"),
    _r(32073, "battery_error2", "Battery error 2"),
    _r(32074, "battery_error3", "Battery error 3"),
    _r(32075, "battery_warning1", "Battery warning 1"),
    _r(32076, "battery_warning2", "Battery warning 2"),
    _r(32077, "battery_warning3", "Battery warning 3"),
    _r(32078, "battery_protection1", "Battery protection 1"),
    _r(32079, "battery_protection2", "Battery protection 2"),
    _r(32080, "battery_protection3", "Battery protection 3"),
    _r(32081, "battery_cycles", "Cycles"),
    _r(32082, "max_charge_voltage", "Max charge voltage", unit="V", scale=0.1),
    _r(32083, "max_charge_current", "Max charge current", unit="A", scale=0.01),
    _r(32084, "min_discharge_voltage", "Min discharge voltage", unit="V", scale=0.1),
    _r(32085, "max_discharge_current", "Max discharge current", dtype=S16,
       unit="A", scale=0.01),
    _r(32086, "highest_cell_voltage", "Highest cell voltage", unit="V", scale=0.001),
    _r(32087, "highest_voltage_cell", "Highest voltage cell No."),
    _r(32088, "lowest_cell_voltage", "Lowest cell voltage", unit="V", scale=0.001),
    _r(32089, "lowest_voltage_cell", "Lowest voltage cell No."),
    _r(32090, "highest_cell_temperature", "Highest cell temperature",
       dtype=S16, unit="°C", scale=0.1),
    _r(32091, "highest_temperature_cell", "Highest temperature cell No."),
    _r(32092, "lowest_cell_temperature", "Lowest cell temperature",
       dtype=S16, unit="°C", scale=0.1),
    _r(32093, "lowest_temperature_cell", "Lowest temperature cell No."),

    # ---- battery2_parameters ------------------------------------
    # v0.0.7 section 5.2.9 (added by change D005, 2025-03-04).  A verbatim
    # mirror of the battery1 block at +200, valid only on inverters with two
    # BMS interfaces; on single-battery units the block reads as zeros or
    # fails outright, exactly like any other undocumented address.
    _r(32240, "battery2_status", "Battery 2 status", enum=BATTERY_STATUS,
       notes="second battery; valid only if the inverter has two BMS "
             "interfaces"),
    _r(32251, "battery2_bms_status", "BMS 2 communication status", enum=BMS_STATUS),
    _r(32255, "battery2_voltage", "Battery 2 voltage", unit="V", scale=0.1),
    _r(32256, "battery2_current", "Battery 2 current", dtype=S16,
       unit="A", scale=0.01, notes="positive = discharge"),
    _r(32257, "battery2_power", "Battery 2 power", dtype=S32, unit="W",
       notes="positive = discharge"),
    _r(32263, "battery2_temperature", "Battery 2 temperature", dtype=S16,
       unit="°C", scale=0.1),
    _r(32264, "battery2_soc", "Battery 2 SOC", unit="%"),
    _r(32265, "battery2_soh", "Battery 2 SOH", unit="%",
       notes="the document prints this address as 322065, an obvious typo "
             "for 32265"),
    _r(32269, "battery2_forbid_charge", "Battery 2 forbid charge flag",
       enum={0: "chargeable", 1: "charge forbidden"}),
    _r(32270, "battery2_forbid_discharge", "Battery 2 forbid discharge flag",
       enum={0: "dischargeable", 1: "discharge forbidden"}),
    _r(32271, "battery2_forced_mode_request",
       "Battery 2 request forced charge or discharge",
       enum={0: "neither", 1: "normal charge", 2: "forced charge"}),
    _r(32272, "battery2_error1", "Battery 2 error 1"),
    _r(32273, "battery2_error2", "Battery 2 error 2"),
    _r(32274, "battery2_error3", "Battery 2 error 3"),
    _r(32275, "battery2_warning1", "Battery 2 warning 1"),
    _r(32276, "battery2_warning2", "Battery 2 warning 2"),
    _r(32277, "battery2_warning3", "Battery 2 warning 3"),
    _r(32278, "battery2_protection1", "Battery 2 protection 1"),
    _r(32279, "battery2_protection2", "Battery 2 protection 2"),
    _r(32280, "battery2_protection3", "Battery 2 protection 3"),
    _r(32281, "battery2_cycles", "Battery 2 cycles"),
    _r(32282, "battery2_max_charge_voltage", "Battery 2 max charge voltage",
       unit="V", scale=0.1),
    _r(32283, "battery2_max_charge_current", "Battery 2 max charge current",
       unit="A", scale=0.01),
    _r(32284, "battery2_min_discharge_voltage", "Battery 2 min discharge voltage",
       unit="V", scale=0.1),
    _r(32285, "battery2_max_discharge_current", "Battery 2 max discharge current",
       dtype=S16, unit="A", scale=0.01),
    _r(32286, "battery2_highest_cell_voltage", "Battery 2 highest cell voltage",
       unit="V", scale=0.001),
    _r(32287, "battery2_highest_voltage_cell", "Battery 2 highest voltage cell No."),
    _r(32288, "battery2_lowest_cell_voltage", "Battery 2 lowest cell voltage",
       unit="V", scale=0.001),
    _r(32289, "battery2_lowest_voltage_cell", "Battery 2 lowest voltage cell No."),
    _r(32290, "battery2_highest_cell_temperature",
       "Battery 2 highest cell temperature", dtype=S16, unit="°C", scale=0.1),
    _r(32291, "battery2_highest_temperature_cell",
       "Battery 2 highest temperature cell No."),
    _r(32292, "battery2_lowest_cell_temperature",
       "Battery 2 lowest cell temperature", dtype=S16, unit="°C", scale=0.1),
    _r(32293, "battery2_lowest_temperature_cell",
       "Battery 2 lowest temperature cell No."),

    # ---- meter_parameters ---------------------------------------
    _r(33001, "meter_r_voltage", "Meter R-phase voltage", unit="V", scale=0.1),
    _r(33002, "meter_r_current", "Meter R-phase current", dtype=S16,
       unit="A", scale=0.1,
       notes="the spec's 0.1 scale is doubtful: 14 -> 1.4 A implies "
             "V*I ~= 327 VA while total P reads 0-1 W and Q reads 25 var "
             "(S ~= 25 VA). A 0.01 scale would give 0.14 A -> 33 VA, which "
             "fits. Voltage and power both agree with the vendor cloud"),
    _r(33003, "meter_r_frequency", "Meter R-phase frequency", unit="Hz", scale=0.01),
    _r(33004, "meter_r_power", "Meter R-phase power", dtype=S32, unit="W"),
    _r(33006, "meter_s_voltage", "Meter S-phase voltage", unit="V", scale=0.1),
    _r(33007, "meter_s_current", "Meter S-phase current", dtype=S16,
       unit="A", scale=0.1),
    _r(33008, "meter_s_frequency", "Meter S-phase frequency", unit="Hz", scale=0.01),
    _r(33009, "meter_s_power", "Meter S-phase power", dtype=S32, unit="W"),
    _r(33011, "meter_t_voltage", "Meter T-phase voltage", unit="V", scale=0.1),
    _r(33012, "meter_t_current", "Meter T-phase current", dtype=S16,
       unit="A", scale=0.1),
    _r(33013, "meter_t_frequency", "Meter T-phase frequency", unit="Hz", scale=0.01),
    _r(33014, "meter_t_power", "Meter T-phase power", dtype=S32, unit="W"),
    _r(33016, "meter_total_p", "Meter total P", dtype=S32, unit="W",
       notes="positive = generation, negative = consumption"),
    _r(33018, "meter_total_q", "Meter total Q", dtype=S32, unit="var"),
    _r(33020, "meter_power_factor", "Meter power factor", dtype=S16, scale=0.01,
       notes="self-consistent, unlike the inverter's 31022: with P at 0-1 W and "
             "Q at 25 var, P/sqrt(P^2+Q^2) is ~0.00-0.04 and the register reads "
             "0.00. Any small value here just means grid exchange is near zero, "
             "so this number carries no information in self-consumption mode"),
    _r(33040, "energy_to_grid_total", "E-Total to grid", unit="kWh", scale=0.1,
       dtype=U32, notes="sell to grid"),
    _r(33042, "energy_from_grid_total", "E-Total from grid", unit="kWh", scale=0.1,
       dtype=U32, notes="buy from grid"),
    # v0.0.7 additions (change D007, 2025-06-11): the meter self-test status.
    _r(33048, "meter_testing_process", "Meter testing process", unit="%",
       notes="0~100, progress of the meter self test"),
    _r(33049, "meter_testing_result", "Result of meter testing",
       enum=METER_TEST_RESULT),

    # ---- meter2_parameters --------------------------------------
    # v0.0.7 section 5.3.1 (added by change D009, 2026-10-24): a verbatim
    # mirror of the meter1 block at +100, for the optional second meter.
    _r(33101, "meter2_r_voltage", "Meter 2 R-phase voltage", unit="V", scale=0.1),
    _r(33102, "meter2_r_current", "Meter 2 R-phase current", dtype=S16,
       unit="A", scale=0.1,
       notes="same doubtful 0.1 scale as meter1's 33002"),
    _r(33103, "meter2_r_frequency", "Meter 2 R-phase frequency",
       unit="Hz", scale=0.01),
    _r(33104, "meter2_r_power", "Meter 2 R-phase power", dtype=S32, unit="W"),
    _r(33106, "meter2_s_voltage", "Meter 2 S-phase voltage", unit="V", scale=0.1),
    _r(33107, "meter2_s_current", "Meter 2 S-phase current", dtype=S16,
       unit="A", scale=0.1),
    _r(33108, "meter2_s_frequency", "Meter 2 S-phase frequency",
       unit="Hz", scale=0.01),
    _r(33109, "meter2_s_power", "Meter 2 S-phase power", dtype=S32, unit="W"),
    _r(33111, "meter2_t_voltage", "Meter 2 T-phase voltage", unit="V", scale=0.1),
    _r(33112, "meter2_t_current", "Meter 2 T-phase current", dtype=S16,
       unit="A", scale=0.1),
    _r(33113, "meter2_t_frequency", "Meter 2 T-phase frequency",
       unit="Hz", scale=0.01),
    _r(33114, "meter2_t_power", "Meter 2 T-phase power", dtype=S32, unit="W"),
    _r(33116, "meter2_total_p", "Meter 2 total P", dtype=S32, unit="W",
       notes="positive = generation, negative = consumption"),
    _r(33118, "meter2_total_q", "Meter 2 total Q", dtype=S32, unit="var"),
    _r(33120, "meter2_power_factor", "Meter 2 power factor", dtype=S16, scale=0.01),
    _r(33140, "meter2_energy_to_grid_total", "Meter 2 E-Total to grid",
       unit="kWh", scale=0.1, dtype=U32, notes="sell to grid"),
    _r(33142, "meter2_energy_from_grid_total", "Meter 2 E-Total from grid",
       unit="kWh", scale=0.1, dtype=U32, notes="buy from grid"),

    # ---- generation_info ------------------------------------------
    # Every *_total below is U32 and was first written without dtype=U32, which
    # made it read a single register -- the high word, which is always 0 -- and
    # report 0 for the whole lifetime counter.  The low word (address + 1) is
    # where the magnitude actually lives.
    _r(34001, "energy_from_inverter_total", "E-Total from inverter",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34003, "energy_to_inverter_total", "E-Total to inverter",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34005, "energy_from_pv_total", "E-Total from PV", unit="kWh", scale=0.1,
       dtype=U32),
    _r(34007, "energy_to_battery_total", "E-Total to battery", unit="kWh",
       scale=0.1, dtype=U32),
    _r(34009, "energy_from_battery_total", "E-Total from battery",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34011, "energy_grid_to_battery_total", "E-Total from grid to battery",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34013, "energy_to_eps_total", "E-Total to EPS", unit="kWh", scale=0.1,
       dtype=U32),
    _r(34015, "hours_generation_total", "H-Total of generation", unit="h",
       dtype=U32),
    _r(34019, "hours_eps_total", "H-Total of EPS", unit="h", dtype=U32),
    _r(34021, "energy_today_dc", "E-today (DC generation)", unit="kWh", scale=0.1),
    _r(34022, "energy_today_battery_charge", "E-today (battery charge)",
       unit="kWh", scale=0.1),
    _r(34023, "energy_today_battery_discharge", "E-today (battery discharge)",
       unit="kWh", scale=0.1),
    _r(34024, "energy_today_grid_charge", "E-today (grid charge)",
       unit="kWh", scale=0.1),
    _r(34025, "energy_today_sell_to_grid", "E-today (sell to grid)",
       unit="kWh", scale=0.1),
    _r(34026, "energy_today_buy_from_grid", "E-today (buy from grid)",
       unit="kWh", scale=0.1),
    _r(34027, "energy_today_eps", "E-today (EPS consumption)", unit="kWh", scale=0.1),
    _r(34028, "hours_today_generation", "H-Today of generation", unit="h", scale=0.1),
    _r(34029, "energy_today_battery_from_pv", "E-today (battery charge from PV)",
       unit="kWh", scale=0.1),
    _r(34030, "energy_today_eps_from_pv", "E-today (EPS consume PV power)",
       unit="kWh", scale=0.1),
    _r(34031, "energy_today_pv_sell_to_grid", "E-today (PV sell to grid)",
       unit="kWh", scale=0.1),
    _r(34032, "energy_today_battery_sell_to_grid", "E-today (battery sell to grid)",
       unit="kWh", scale=0.1),
    _r(34033, "energy_today_battery_eps", "E-today (battery power EPS)",
       unit="kWh", scale=0.1),
    _r(34034, "energy_total_pv_charge_battery", "E-total (PV charge battery)",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34036, "energy_total_eps_from_pv", "E-total (EPS consume PV)",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34038, "energy_total_pv_sell_to_grid", "E-total (PV sell to grid)",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34040, "energy_total_battery_to_grid", "E-total (battery discharge to grid)",
       unit="kWh", scale=0.1, dtype=U32),
    _r(34042, "energy_total_battery_to_eps", "E-total (battery discharge to EPS)",
       unit="kWh", scale=0.1, dtype=U32),

    # ---- Italy_self-test ----------------------------------------
    _r(35001, "ips_result", "IPS result",
       enum={0: "invalid", 1: "success", 2: "failure"}),
    _r(35002, "ov_test_voltage_s1", "59.S1 ten minute overvoltage test voltage",
       unit="V", scale=0.1),
    _r(35003, "ov_test_time_s1", "59.S1 ten minute overvoltage test time",
       unit="s", scale=0.1),
    _r(35004, "ov_test_max_voltage_s2", "59.S2 max voltage in overvoltage test",
       unit="V", scale=0.1),
    _r(35005, "ov_test_time_s2", "59.S2 overvoltage test time", unit="s", scale=0.1),
    _r(35006, "uv_test_min_voltage_s1", "27.S1 min voltage in undervoltage test",
       unit="V", scale=0.1),
    _r(35007, "uv_test_time_s1", "27.S1 undervoltage test time", unit="s", scale=0.1),
    _r(35008, "uv_test_min_voltage_s2", "27.S2 min voltage in undervoltage test",
       unit="V", scale=0.1),
    _r(35009, "uv_test_time_s2", "27.S2 undervoltage test time", unit="s", scale=0.1),
    _r(35010, "of_test_max_freq_s1", "81>.S1 max frequency in overfrequency test",
       unit="Hz", scale=0.01),
    _r(35011, "of_test_time_s1", "81>.S1 max time in overfrequency test",
       unit="s", scale=0.1),
    _r(35012, "uf_test_min_freq_s1", "81<.S1 min frequency in underfrequency test",
       unit="Hz", scale=0.01),
    _r(35013, "uf_test_time_s1", "81<.S1 min time in underfrequency test",
       unit="s", scale=0.1),
    _r(35014, "of_test_max_freq_s2", "81>.S2 max frequency in overfrequency test",
       unit="Hz", scale=0.01),
    _r(35015, "of_test_time_s2", "81>.S2 max time in overfrequency test",
       unit="s", scale=0.1),
    _r(35016, "uf_test_min_freq_s2", "81<.S2 min frequency in underfrequency test",
       unit="Hz", scale=0.01),
    _r(35017, "uf_test_time_s2", "81<.S2 max time in underfrequency test",
       unit="s", scale=0.1),
)


SECTION_ORDER = tuple(dict.fromkeys(name for _, _, name in SECTION_RANGES))


def by_section() -> dict[str, list[Register]]:
    """Registers grouped by protocol-document section, in document order."""
    out: dict[str, list[Register]] = {}
    for reg in REGISTERS:
        out.setdefault(reg.section, []).append(reg)
    return out


def blocks(max_registers: int = 50) -> list[tuple[int, int]]:
    """Merge neighbouring registers into contiguous read ranges.

    Only addresses that actually carry a documented value are read, so
    reserved gaps split a block.  Each block is capped at *max_registers*
    because the vendor recommends frames of at most 100 bytes.
    """
    if max_registers < 1:
        raise ValueError("max_registers must be positive")

    addresses: set[int] = set()
    for reg in REGISTERS:
        addresses.update(range(reg.address, reg.end + 1))

    ordered = sorted(addresses)
    runs: list[tuple[int, int]] = []
    for addr in ordered:
        if runs and addr == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], addr)
        else:
            runs.append((addr, addr))

    split: list[tuple[int, int]] = []
    for start, end in runs:
        while end - start + 1 > max_registers:
            split.append((start, start + max_registers - 1))
            start += max_registers
        split.append((start, end))
    return split
