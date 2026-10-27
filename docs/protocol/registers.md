---
title: Register Map
icon: database
order: -6
---

# Register Map

Read-only **input registers** `30000`–`35017`, transcribed from the
*FuturaPulse hybrid inverter Modbus protocol v0.0.7*.

**235 values** across **42 coalesced read ranges**, in fourteen sections matching
the chapters of the source document.

!!!success This project never writes
The writable holding registers (`40001`–`49999`, FC `0x03`/`0x06`/`0x10`)
are **deliberately absent** from this map. Only function codes `0x04` and
`0x03` reads are implemented.
!!!

---

## Addressing

Document register numbers are used **verbatim** as the Modbus PDU address — no
`30000` offset is subtracted.

That is confirmed twice over: by the source document's own example frames
(`01 04 75 94 …` reads register `30100`) and by live traffic from a Si-Dongle-L.

```bash
# reads register 31002 directly, no offset arithmetic
sitsolar-dongle <host> --sn ... --read 31002:2
```

---

## Word order {#word-order}

32-bit values are **big-endian pairs**: the **low** address holds the *high*
word.

```text
31002~31003  ->  (0, 50010)  ->  0x0000C352  ->  50.010 Hz
                 high  low
```

!!!warning This is an easy way to silently read zero
Because the high word sits at the low address, reading a single 16-bit
register out of a 32-bit pair returns only the high word. For lifetime
counters that word is frequently `0`, so the value reads as zero while the
real figure sits one register over.

This is exactly the bug that was fixed: every lifetime total was reported as
zero. As a cross-check, the decoded totals now match the vendor cloud
exactly — `all_charge` 600.2, `all_discharge` 570.1,
`all_pv_to_battery_level` 437.6, `all_battery_to_grid_capacity` 317.7,
`totalOngridPower` 446.5, `totalBuyGridPower` 51.8 kWh.
!!!

---

## Data types

| Type | Registers | Meaning |
|---|---|---|
| `U16` | 1 | Unsigned 16-bit integer. |
| `S16` | 1 | Two's complement signed 16-bit integer. |
| `U32` | 2 | Unsigned, high word first. |
| `S32` | 2 | Signed, high word first. |
| `ASCII` | `length` | Two characters per register, NUL padded. |
| `BITFIELD` | 1 | Each bit is an independent flag. |

The `scale` column converts the raw integer into the `unit`. For example
`31000` `phase_r_voltage` has scale `0.1` and unit `V`, so a raw `2301` decodes
as `230.1 V`.

Enums and bit fields are decoded into human-readable names: enumerations become
`label`, and bit fields become the `active_bits` list. Run
`sitsolar-dongle --list` to see every register with its scale, unit, and notes.

---

## Sections

| Section | Range | Values |
|---|---|---|
| `device_info` | `30000–30082` | 14 |
| `operation_data` | `30100–30109` | 8 |
| `error_info` | `30200–30211` | 12 |
| `temperatures` | `30301–30316` | 5 |
| `ARM_data` | `30400–30421` | 8 |
| `DC_side` | `30500–30508` | 9 |
| `AC_side` | `31000–31040` | 18 |
| `EPS_parameters` | `32001–32026` | 15 |
| `battery_parameters` | `32040–32093` | 33 |
| `battery2_parameters` | `32240–32293` | 33 |
| `meter_parameters` | `33001–33049` | 19 |
| `meter2_parameters` | `33101–33143` | 17 |
| `generation_info` | `34001–34043` | 27 |
| `Italy_self-test` | `35001–35017` | 17 |

Use `--section` to poll one of these in isolation, e.g.
`--section "AC_side"`.

---

## Full map

The tables below are generated directly from
[`registers.py`](https://github.com/ch3p4ll3/sitsolar-dongle/blob/main/src/sitsolar_dongle/registers.py),
so they cannot drift from the implementation.


### device_info

`30000–30099` — 14 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30000` | `device_type` | Device type | U16 |  |  | 6 enum labels |
| `30001~30010` | `serial_number` | Serial number | ASCII (10) |  |  |  |
| `30011~30019` | `model` | Inverter model | ASCII (9) |  |  |  |
| `30020~30025` | `brand` | Brand info | ASCII (6) |  |  |  |
| `30026~30033` | `manufacturer` | Manufacturer | ASCII (8) |  |  |  |
| `30034` | `inverter_version` | Inverter version | U16 |  |  |  |
| `30041~30045` | `dsp1_version` | DSP1 version | ASCII (5) |  |  |  |
| `30046~30050` | `dsp2_version` | DSP2 version | ASCII (5) |  |  |  |
| `30061` | `dsp1_bootloader` | DSP1 bootloader version | U16 |  |  |  |
| `30062` | `dsp2_bootloader` | DSP2 bootloader version | U16 |  |  |  |
| `30070~30074` | `dsp_protocol_version` | DSP protocol version | ASCII (5) |  |  |  |
| `30075~30079` | `safety_code_version` | Safety code version | ASCII (5) |  |  |  |
| `30080` | `modbus_id` | Modbus ID | U16 |  |  |  |
| `30081~30082` | `rated_power` | Rated power | U32 |  | W |  |


### operation_data

`30100–30199` — 8 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30100` | `operation_status` | Operation status | U16 |  |  | 7 enum labels |
| `30101` | `auto_start_remaining` | Auto start time remaining | U16 |  | s |  |
| `30103` | `mppt_input_type` | MPPT input type | U16 |  |  | v0.0.3 listed five values (no input / single string connected / two strings parallel / different panels parallel / same panels parallel); v0.0.7 reduced them to two. 2 enum labels |
| `30104` | `mppt_string_number` | MPPT string number | U16 |  |  |  |
| `30105` | `pv_string_number` | PV string number | U16 |  |  |  |
| `30106` | `dc_power_flags` | DC power lose or updating signs | BITFIELD |  |  | 2 named bits |
| `30108` | `safety_code_changed` | Safety code change flag bit | U16 |  |  | 2 enum labels |
| `30109` | `dsp_ac_power_off` | DSP AC power off | U16 |  |  | 2 enum labels |


### error_info

`30200–30299` — 12 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30200` | `dsp_error1` | DSP error 1 | U16 |  |  |  |
| `30201` | `dsp_error2` | DSP error 2 | U16 |  |  |  |
| `30202` | `dsp_error3` | DSP error 3 | U16 |  |  |  |
| `30203` | `dsp_error4` | DSP error 4 | U16 |  |  |  |
| `30204` | `dsp_error5` | DSP error 5 | U16 |  |  |  |
| `30205` | `dsp_error6` | DSP error 6 | U16 |  |  |  |
| `30206` | `dsp_error7` | DSP error 7 | U16 |  |  |  |
| `30207` | `dsp_warning1` | DSP warning 1 | BITFIELD |  |  | 14 named bits |
| `30208` | `dsp_warning2` | DSP warning 2 | BITFIELD |  |  | 10 named bits |
| `30209` | `dsp_warning3` | DSP warning 3 | BITFIELD |  |  | 11 named bits |
| `30210` | `dsp_warning4` | DSP warning 4 | BITFIELD |  |  | the protocol document gives no chart for 30210, so its bits are unknown; codes 249+ would belong to this word |
| `30211` | `dsp_warning5` | DSP warning 5 | U16 |  |  | Chart 11: carries a decimal warning code, not a bit mask. 200 means no warning. 25 enum labels |


### temperatures

`30300–30399` — 5 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30301` | `cabin_temperature` | Cabin temperature | S16 | `0.1` | °C |  |
| `30303` | `boost_module1_temperature` | Boost module 1 temperature | S16 | `0.1` | °C |  |
| `30305` | `buckboost_temperature` | Buckboost temperature | S16 | `0.1` | °C |  |
| `30307` | `dc_ac_module_temperature` | DC/AC module temperature | S16 | `0.1` | °C |  |
| `30316` | `heat_sink_temperature` | Heat sink temperature | S16 | `0.1` | °C |  |


### ARM_data

`30400–30499` — 8 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30400~30404` | `arm_protocol_version` | ARM protocol version | ASCII (5) |  |  |  |
| `30405~30406` | `arm_software_code_id` | ARM software code ID | ASCII (2) |  |  |  |
| `30407~30411` | `arm_software_version` | ARM software version | ASCII (5) |  |  |  |
| `30412~30416` | `bms_software_version` | BMS software version | ASCII (5) |  |  | depends on battery |
| `30417` | `arm_bootloader_version` | ARM bootloader version | U16 |  |  |  |
| `30419` | `arm_error` | ARM error code | U16 |  |  |  |
| `30420` | `arm_bit_errors1` | ARM bit errors 1 | BITFIELD |  |  |  |
| `30421` | `arm_bit_errors2` | ARM bit errors 2 | BITFIELD |  |  |  |


### DC_side

`30500–30999` — 9 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `30500` | `total_pv_power` | Total PV power | U16 | `0.01` | kW |  |
| `30501` | `pv1_voltage` | PV 1 voltage | U16 | `0.1` | V |  |
| `30502` | `pv1_current` | PV 1 current | U16 | `0.1` | A |  |
| `30503` | `pv1_power` | PV 1 power | U16 | `0.01` | kW |  |
| `30504` | `pv1_energy_today` | E-Today (PV 1) | U16 | `0.1` | kWh |  |
| `30505` | `pv2_voltage` | PV 2 voltage | U16 | `0.1` | V |  |
| `30506` | `pv2_current` | PV 2 current | U16 | `0.1` | A |  |
| `30507` | `pv2_power` | PV 2 power | U16 | `0.01` | kW |  |
| `30508` | `pv2_energy_today` | E-Today (PV 2) | U16 | `0.1` | kWh |  |


### AC_side

`31000–31999` — 18 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `31000` | `phase_r_voltage` | R-phase voltage | U16 | `0.1` | V |  |
| `31001` | `phase_r_current` | R-phase current | U16 | `0.01` | A |  |
| `31002~31003` | `grid_frequency` | Grid frequency | U32 | `0.001` | Hz |  |
| `31004~31005` | `phase_r_power` | R-phase power | S32 |  | W |  |
| `31006` | `phase_s_voltage` | S-phase voltage | U16 | `0.1` | V |  |
| `31007` | `phase_s_current` | S-phase current | U16 | `0.01` | A |  |
| `31010~31011` | `phase_s_power` | S-phase power | S32 |  | W |  |
| `31012` | `phase_t_voltage` | T-phase voltage | U16 | `0.1` | V |  |
| `31013` | `phase_t_current` | T-phase current | U16 | `0.01` | A |  |
| `31016~31017` | `phase_t_power` | T-phase power | S32 |  | W | the document's table says S16 but gives the range 31016~31017; the R and S phases are S32 at the same offset, so treat as S32 |
| `31018~31019` | `inverter_p` | P_Inv | S32 |  | W |  |
| `31020~31021` | `inverter_q` | Q_Inv | S32 |  | var |  |
| `31022` | `power_factor` | Power factor | S16 | `0.001` |  | reads a near-constant 0.999 and equals P/(31039~31040) to within 0.005, but contradicts P/sqrt(P^2+Q^2) ~= 0.89 measured on the same poll. The vendor's own cloud publishes the same 0.999, so this is a firmware/setpoint value, not the instantaneous PF (the spec has a separate writable 46056 Power factors adjust) |
| `31035` | `grid_line_rs` | RS grid line voltage | U16 | `0.1` | V |  |
| `31036` | `grid_line_st` | ST grid line voltage | U16 | `0.1` | V |  |
| `31037` | `grid_line_tr` | TR grid line voltage | U16 | `0.1` | V |  |
| `31038` | `grid_n_pe_voltage` | Grid N-PE voltage | S16 | `0.1` | V |  |
| `31039~31040` | `apparent_power` | Inverter output apparent power | U32 |  | VA | the spec calls this apparent power, but it tracks P (31018~31019) within 1-2 W: 419 VA vs 417 W, 425 VA vs 424 W, i.e. it is P/power_factor. True apparent power is V*I and sqrt(P^2+Q^2), which agree with each other at ~470 VA -- about 50 VA higher. Do not use as S for a VA sensor; compute sqrt(P^2+Q^2) instead |


### EPS_parameters

`32000–32039` — 15 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `32001` | `eps_voltage_r` | EPS voltage R | U16 | `0.1` | V |  |
| `32002` | `eps_current_r` | EPS current R | U16 | `0.1` | A |  |
| `32005~32006` | `eps_p_r` | EPS P_R | S32 |  | W |  |
| `32007` | `eps_voltage_s` | EPS voltage S | U16 | `0.1` | V |  |
| `32008` | `eps_current_s` | EPS current S | U16 | `0.1` | A |  |
| `32011~32012` | `eps_p_s` | EPS P_S | S32 |  | W |  |
| `32013` | `eps_voltage_t` | EPS voltage T | U16 | `0.1` | V |  |
| `32014` | `eps_current_t` | EPS current T | U16 | `0.1` | A |  |
| `32017~32018` | `eps_p_t` | EPS P_T | S32 |  | W |  |
| `32019~32020` | `eps_total_p` | EPS total P | S32 |  | W |  |
| `32021~32022` | `eps_total_q` | EPS total Q | S32 |  | var |  |
| `32023` | `eps_voltage_rs` | EPS Voltage RS | U16 | `0.1` | V |  |
| `32024` | `eps_voltage_st` | EPS Voltage ST | U16 | `0.1` | V |  |
| `32025` | `eps_voltage_tr` | EPS Voltage TR | U16 | `0.1` | V |  |
| `32026` | `eps_voltage_n_pe` | EPS Voltage N_PE | U16 | `0.1` | V |  |


### battery_parameters

`32040–32239` — 33 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `32040` | `battery_status` | Battery status | U16 |  |  | 4 enum labels |
| `32051` | `bms_status` | BMS communication status | U16 |  |  | 3 enum labels |
| `32055` | `battery_voltage` | Battery voltage | U16 | `0.1` | V |  |
| `32056` | `battery_current` | Battery current | S16 | `0.01` | A | positive = discharge |
| `32057~32058` | `battery_power` | Battery power | S32 |  | W | positive = discharge |
| `32063` | `battery_temperature` | Battery temperature | S16 | `0.1` | °C |  |
| `32064` | `battery_soc` | Battery SOC | U16 |  | % |  |
| `32065` | `battery_soh` | Battery SOH | U16 |  | % |  |
| `32069` | `forbid_charge` | Forbid charge flag | U16 |  |  | 2 enum labels |
| `32070` | `forbid_discharge` | Forbid discharge flag | U16 |  |  | 2 enum labels |
| `32071` | `forced_mode_request` | Battery request forced charge or discharge | U16 |  |  | 3 enum labels |
| `32072` | `battery_error1` | Battery error 1 | U16 |  |  |  |
| `32073` | `battery_error2` | Battery error 2 | U16 |  |  |  |
| `32074` | `battery_error3` | Battery error 3 | U16 |  |  |  |
| `32075` | `battery_warning1` | Battery warning 1 | U16 |  |  |  |
| `32076` | `battery_warning2` | Battery warning 2 | U16 |  |  |  |
| `32077` | `battery_warning3` | Battery warning 3 | U16 |  |  |  |
| `32078` | `battery_protection1` | Battery protection 1 | U16 |  |  |  |
| `32079` | `battery_protection2` | Battery protection 2 | U16 |  |  |  |
| `32080` | `battery_protection3` | Battery protection 3 | U16 |  |  |  |
| `32081` | `battery_cycles` | Cycles | U16 |  |  |  |
| `32082` | `max_charge_voltage` | Max charge voltage | U16 | `0.1` | V |  |
| `32083` | `max_charge_current` | Max charge current | U16 | `0.01` | A |  |
| `32084` | `min_discharge_voltage` | Min discharge voltage | U16 | `0.1` | V |  |
| `32085` | `max_discharge_current` | Max discharge current | S16 | `0.01` | A |  |
| `32086` | `highest_cell_voltage` | Highest cell voltage | U16 | `0.001` | V |  |
| `32087` | `highest_voltage_cell` | Highest voltage cell No. | U16 |  |  |  |
| `32088` | `lowest_cell_voltage` | Lowest cell voltage | U16 | `0.001` | V |  |
| `32089` | `lowest_voltage_cell` | Lowest voltage cell No. | U16 |  |  |  |
| `32090` | `highest_cell_temperature` | Highest cell temperature | S16 | `0.1` | °C |  |
| `32091` | `highest_temperature_cell` | Highest temperature cell No. | U16 |  |  |  |
| `32092` | `lowest_cell_temperature` | Lowest cell temperature | S16 | `0.1` | °C |  |
| `32093` | `lowest_temperature_cell` | Lowest temperature cell No. | U16 |  |  |  |


### battery2_parameters

`32240–32299` — 33 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `32240` | `battery2_status` | Battery 2 status | U16 |  |  | second battery; valid only if the inverter has two BMS interfaces. 4 enum labels |
| `32251` | `battery2_bms_status` | BMS 2 communication status | U16 |  |  | 3 enum labels |
| `32255` | `battery2_voltage` | Battery 2 voltage | U16 | `0.1` | V |  |
| `32256` | `battery2_current` | Battery 2 current | S16 | `0.01` | A | positive = discharge |
| `32257~32258` | `battery2_power` | Battery 2 power | S32 |  | W | positive = discharge |
| `32263` | `battery2_temperature` | Battery 2 temperature | S16 | `0.1` | °C |  |
| `32264` | `battery2_soc` | Battery 2 SOC | U16 |  | % |  |
| `32265` | `battery2_soh` | Battery 2 SOH | U16 |  | % | the document prints this address as 322065, an obvious typo for 32265 |
| `32269` | `battery2_forbid_charge` | Battery 2 forbid charge flag | U16 |  |  | 2 enum labels |
| `32270` | `battery2_forbid_discharge` | Battery 2 forbid discharge flag | U16 |  |  | 2 enum labels |
| `32271` | `battery2_forced_mode_request` | Battery 2 request forced charge or discharge | U16 |  |  | 3 enum labels |
| `32272` | `battery2_error1` | Battery 2 error 1 | U16 |  |  |  |
| `32273` | `battery2_error2` | Battery 2 error 2 | U16 |  |  |  |
| `32274` | `battery2_error3` | Battery 2 error 3 | U16 |  |  |  |
| `32275` | `battery2_warning1` | Battery 2 warning 1 | U16 |  |  |  |
| `32276` | `battery2_warning2` | Battery 2 warning 2 | U16 |  |  |  |
| `32277` | `battery2_warning3` | Battery 2 warning 3 | U16 |  |  |  |
| `32278` | `battery2_protection1` | Battery 2 protection 1 | U16 |  |  |  |
| `32279` | `battery2_protection2` | Battery 2 protection 2 | U16 |  |  |  |
| `32280` | `battery2_protection3` | Battery 2 protection 3 | U16 |  |  |  |
| `32281` | `battery2_cycles` | Battery 2 cycles | U16 |  |  |  |
| `32282` | `battery2_max_charge_voltage` | Battery 2 max charge voltage | U16 | `0.1` | V |  |
| `32283` | `battery2_max_charge_current` | Battery 2 max charge current | U16 | `0.01` | A |  |
| `32284` | `battery2_min_discharge_voltage` | Battery 2 min discharge voltage | U16 | `0.1` | V |  |
| `32285` | `battery2_max_discharge_current` | Battery 2 max discharge current | S16 | `0.01` | A |  |
| `32286` | `battery2_highest_cell_voltage` | Battery 2 highest cell voltage | U16 | `0.001` | V |  |
| `32287` | `battery2_highest_voltage_cell` | Battery 2 highest voltage cell No. | U16 |  |  |  |
| `32288` | `battery2_lowest_cell_voltage` | Battery 2 lowest cell voltage | U16 | `0.001` | V |  |
| `32289` | `battery2_lowest_voltage_cell` | Battery 2 lowest voltage cell No. | U16 |  |  |  |
| `32290` | `battery2_highest_cell_temperature` | Battery 2 highest cell temperature | S16 | `0.1` | °C |  |
| `32291` | `battery2_highest_temperature_cell` | Battery 2 highest temperature cell No. | U16 |  |  |  |
| `32292` | `battery2_lowest_cell_temperature` | Battery 2 lowest cell temperature | S16 | `0.1` | °C |  |
| `32293` | `battery2_lowest_temperature_cell` | Battery 2 lowest temperature cell No. | U16 |  |  |  |


### meter_parameters

`33000–33099` — 19 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `33001` | `meter_r_voltage` | Meter R-phase voltage | U16 | `0.1` | V |  |
| `33002` | `meter_r_current` | Meter R-phase current | S16 | `0.1` | A | the spec's 0.1 scale is doubtful: 14 -> 1.4 A implies V*I ~= 327 VA while total P reads 0-1 W and Q reads 25 var (S ~= 25 VA). A 0.01 scale would give 0.14 A -> 33 VA, which fits. Voltage and power both agree with the vendor cloud |
| `33003` | `meter_r_frequency` | Meter R-phase frequency | U16 | `0.01` | Hz |  |
| `33004~33005` | `meter_r_power` | Meter R-phase power | S32 |  | W |  |
| `33006` | `meter_s_voltage` | Meter S-phase voltage | U16 | `0.1` | V |  |
| `33007` | `meter_s_current` | Meter S-phase current | S16 | `0.1` | A |  |
| `33008` | `meter_s_frequency` | Meter S-phase frequency | U16 | `0.01` | Hz |  |
| `33009~33010` | `meter_s_power` | Meter S-phase power | S32 |  | W |  |
| `33011` | `meter_t_voltage` | Meter T-phase voltage | U16 | `0.1` | V |  |
| `33012` | `meter_t_current` | Meter T-phase current | S16 | `0.1` | A |  |
| `33013` | `meter_t_frequency` | Meter T-phase frequency | U16 | `0.01` | Hz |  |
| `33014~33015` | `meter_t_power` | Meter T-phase power | S32 |  | W |  |
| `33016~33017` | `meter_total_p` | Meter total P | S32 |  | W | positive = generation, negative = consumption |
| `33018~33019` | `meter_total_q` | Meter total Q | S32 |  | var |  |
| `33020` | `meter_power_factor` | Meter power factor | S16 | `0.01` |  | self-consistent, unlike the inverter's 31022: with P at 0-1 W and Q at 25 var, P/sqrt(P^2+Q^2) is ~0.00-0.04 and the register reads 0.00. Any small value here just means grid exchange is near zero, so this number carries no information in self-consumption mode |
| `33040~33041` | `energy_to_grid_total` | E-Total to grid | U32 | `0.1` | kWh | sell to grid |
| `33042~33043` | `energy_from_grid_total` | E-Total from grid | U32 | `0.1` | kWh | buy from grid |
| `33048` | `meter_testing_process` | Meter testing process | U16 |  | % | 0~100, progress of the meter self test |
| `33049` | `meter_testing_result` | Result of meter testing | U16 |  |  | 14 enum labels |


### meter2_parameters

`33100–33199` — 17 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `33101` | `meter2_r_voltage` | Meter 2 R-phase voltage | U16 | `0.1` | V |  |
| `33102` | `meter2_r_current` | Meter 2 R-phase current | S16 | `0.1` | A | same doubtful 0.1 scale as meter1's 33002 |
| `33103` | `meter2_r_frequency` | Meter 2 R-phase frequency | U16 | `0.01` | Hz |  |
| `33104~33105` | `meter2_r_power` | Meter 2 R-phase power | S32 |  | W |  |
| `33106` | `meter2_s_voltage` | Meter 2 S-phase voltage | U16 | `0.1` | V |  |
| `33107` | `meter2_s_current` | Meter 2 S-phase current | S16 | `0.1` | A |  |
| `33108` | `meter2_s_frequency` | Meter 2 S-phase frequency | U16 | `0.01` | Hz |  |
| `33109~33110` | `meter2_s_power` | Meter 2 S-phase power | S32 |  | W |  |
| `33111` | `meter2_t_voltage` | Meter 2 T-phase voltage | U16 | `0.1` | V |  |
| `33112` | `meter2_t_current` | Meter 2 T-phase current | S16 | `0.1` | A |  |
| `33113` | `meter2_t_frequency` | Meter 2 T-phase frequency | U16 | `0.01` | Hz |  |
| `33114~33115` | `meter2_t_power` | Meter 2 T-phase power | S32 |  | W |  |
| `33116~33117` | `meter2_total_p` | Meter 2 total P | S32 |  | W | positive = generation, negative = consumption |
| `33118~33119` | `meter2_total_q` | Meter 2 total Q | S32 |  | var |  |
| `33120` | `meter2_power_factor` | Meter 2 power factor | S16 | `0.01` |  |  |
| `33140~33141` | `meter2_energy_to_grid_total` | Meter 2 E-Total to grid | U32 | `0.1` | kWh | sell to grid |
| `33142~33143` | `meter2_energy_from_grid_total` | Meter 2 E-Total from grid | U32 | `0.1` | kWh | buy from grid |


### generation_info

`34000–34999` — 27 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `34001~34002` | `energy_from_inverter_total` | E-Total from inverter | U32 | `0.1` | kWh |  |
| `34003~34004` | `energy_to_inverter_total` | E-Total to inverter | U32 | `0.1` | kWh |  |
| `34005~34006` | `energy_from_pv_total` | E-Total from PV | U32 | `0.1` | kWh |  |
| `34007~34008` | `energy_to_battery_total` | E-Total to battery | U32 | `0.1` | kWh |  |
| `34009~34010` | `energy_from_battery_total` | E-Total from battery | U32 | `0.1` | kWh |  |
| `34011~34012` | `energy_grid_to_battery_total` | E-Total from grid to battery | U32 | `0.1` | kWh |  |
| `34013~34014` | `energy_to_eps_total` | E-Total to EPS | U32 | `0.1` | kWh |  |
| `34015~34016` | `hours_generation_total` | H-Total of generation | U32 |  | h |  |
| `34019~34020` | `hours_eps_total` | H-Total of EPS | U32 |  | h |  |
| `34021` | `energy_today_dc` | E-today (DC generation) | U16 | `0.1` | kWh |  |
| `34022` | `energy_today_battery_charge` | E-today (battery charge) | U16 | `0.1` | kWh |  |
| `34023` | `energy_today_battery_discharge` | E-today (battery discharge) | U16 | `0.1` | kWh |  |
| `34024` | `energy_today_grid_charge` | E-today (grid charge) | U16 | `0.1` | kWh |  |
| `34025` | `energy_today_sell_to_grid` | E-today (sell to grid) | U16 | `0.1` | kWh |  |
| `34026` | `energy_today_buy_from_grid` | E-today (buy from grid) | U16 | `0.1` | kWh |  |
| `34027` | `energy_today_eps` | E-today (EPS consumption) | U16 | `0.1` | kWh |  |
| `34028` | `hours_today_generation` | H-Today of generation | U16 | `0.1` | h |  |
| `34029` | `energy_today_battery_from_pv` | E-today (battery charge from PV) | U16 | `0.1` | kWh |  |
| `34030` | `energy_today_eps_from_pv` | E-today (EPS consume PV power) | U16 | `0.1` | kWh |  |
| `34031` | `energy_today_pv_sell_to_grid` | E-today (PV sell to grid) | U16 | `0.1` | kWh |  |
| `34032` | `energy_today_battery_sell_to_grid` | E-today (battery sell to grid) | U16 | `0.1` | kWh |  |
| `34033` | `energy_today_battery_eps` | E-today (battery power EPS) | U16 | `0.1` | kWh |  |
| `34034~34035` | `energy_total_pv_charge_battery` | E-total (PV charge battery) | U32 | `0.1` | kWh |  |
| `34036~34037` | `energy_total_eps_from_pv` | E-total (EPS consume PV) | U32 | `0.1` | kWh |  |
| `34038~34039` | `energy_total_pv_sell_to_grid` | E-total (PV sell to grid) | U32 | `0.1` | kWh |  |
| `34040~34041` | `energy_total_battery_to_grid` | E-total (battery discharge to grid) | U32 | `0.1` | kWh |  |
| `34042~34043` | `energy_total_battery_to_eps` | E-total (battery discharge to EPS) | U32 | `0.1` | kWh |  |


### Italy_self-test

`35000–35999` — 17 value(s)

| Address | Key | Name | Type | Scale | Unit | Notes |
|---|---|---|---|---|---|---|
| `35001` | `ips_result` | IPS result | U16 |  |  | 3 enum labels |
| `35002` | `ov_test_voltage_s1` | 59.S1 ten minute overvoltage test voltage | U16 | `0.1` | V |  |
| `35003` | `ov_test_time_s1` | 59.S1 ten minute overvoltage test time | U16 | `0.1` | s |  |
| `35004` | `ov_test_max_voltage_s2` | 59.S2 max voltage in overvoltage test | U16 | `0.1` | V |  |
| `35005` | `ov_test_time_s2` | 59.S2 overvoltage test time | U16 | `0.1` | s |  |
| `35006` | `uv_test_min_voltage_s1` | 27.S1 min voltage in undervoltage test | U16 | `0.1` | V |  |
| `35007` | `uv_test_time_s1` | 27.S1 undervoltage test time | U16 | `0.1` | s |  |
| `35008` | `uv_test_min_voltage_s2` | 27.S2 min voltage in undervoltage test | U16 | `0.1` | V |  |
| `35009` | `uv_test_time_s2` | 27.S2 undervoltage test time | U16 | `0.1` | s |  |
| `35010` | `of_test_max_freq_s1` | 81>.S1 max frequency in overfrequency test | U16 | `0.01` | Hz |  |
| `35011` | `of_test_time_s1` | 81>.S1 max time in overfrequency test | U16 | `0.1` | s |  |
| `35012` | `uf_test_min_freq_s1` | 81<.S1 min frequency in underfrequency test | U16 | `0.01` | Hz |  |
| `35013` | `uf_test_time_s1` | 81<.S1 min time in underfrequency test | U16 | `0.1` | s |  |
| `35014` | `of_test_max_freq_s2` | 81>.S2 max frequency in overfrequency test | U16 | `0.01` | Hz |  |
| `35015` | `of_test_time_s2` | 81>.S2 max time in overfrequency test | U16 | `0.1` | s |  |
| `35016` | `uf_test_min_freq_s2` | 81<.S2 min frequency in underfrequency test | U16 | `0.01` | Hz |  |
| `35017` | `uf_test_time_s2` | 81<.S2 max time in underfrequency test | U16 | `0.1` | s |  |


**Totals**: 235 values across 42 read ranges.

Coalesced read ranges:

```text
30000–30034, 30041–30050, 30061–30062, 30070–30082, 30100–30101, 30103–30106, 30108–30109, 30200–30211, 30301–30301, 30303–30303, 30305–30305, 30307–30307, 30316–30316, 30400–30417, 30419–30421, 30500–30508, 31000–31007, 31010–31013, 31016–31022, 31035–31040, 32001–32002, 32005–32008, 32011–32014, 32017–32026, 32040–32040, 32051–32051, 32055–32058, 32063–32065, 32069–32093, 32240–32240, 32251–32251, 32255–32258, 32263–32265, 32269–32293, 33001–33020, 33040–33043, 33048–33049, 33101–33120, 33140–33143, 34001–34016, 34019–34043, 35001–35017
```

---

## Where the document disagrees with the hardware

Every entry below was checked against a live 3K-HS **and** against the vendor's
own cloud API — which is what the official app displays. Each one is also
recorded in the `notes` field of the corresponding register and printed by
`sitsolar-dongle --list`.

| Register | Document says | What it actually does |
|---|---|---|
| `31039~31040` | Inverter output apparent power | Tracks **P** within 1–2 W (419 VA vs 417 W, 425 VA vs 424 W) — it looks like `P / power_factor`. True apparent power is `V·I`, which agrees with `√(P²+Q²)` at ≈ 470 VA, about 50 VA higher. Compute `√(P²+Q²)` for a VA sensor. |
| `31022` | Power factor | Sits at a near-constant `0.999`. It equals `P / (31039~31040)` but contradicts `P/√(P²+Q²) ≈ 0.89`. The vendor cloud publishes the same `0.999`, so this is a setpoint rather than a measurement — see the writable `46056 Power factors adjust`. |
| `33002` | Meter R-phase current, scale `0.1` | `14 → 1.4 A` implies `V·I ≈ 327 VA` while meter P reads 0–1 W and Q reads 25 var. A `0.01` scale (`0.14 A` → 33 VA) fits instead. Voltage and power both agree with the cloud. |
| `33020` | Meter power factor, scale `0.01` | Self-consistent — it is `≈ P/√(P²+Q²)` with P at a fraction of a watt — but in self-consumption mode grid exchange is near zero, so the number carries no information. |
| `30211` | DSP warning 5, "more details in Appendix 11" | **Not a bit field.** Appendix 1 / Chart 11 is a table of decimal warning codes. |

!!!tip For a VA sensor, compute it yourself
`31039~31040` is not usable as apparent power. Use `√(P²+Q²)` from
`31018~31019` (`inverter_p`) and `31020~31021` (`inverter_q`), which agrees
with `V·I`.
!!!

---

## The warning codes

`30207`–`30210` are four ordinary bit-field words (Charts 8–10). Chart 11
addresses their *combined* space with:

```text
code = 201 + 16·word_index + bit_index
```

That formula reproduces every row of the chart — `201`/`204`/`213` from word 0,
`219`/`225`/`230` from word 1, `241`/`247` from word 2 — which is what shows
`30211` carries a **code rather than a mask**.

!!!success 200 means no warning
The first possible code is `201`, so the value **`200` means no warning at
all** — which is what a healthy unit reports.
!!!

`30210` has no chart in the document, so its bits are unknown; codes `249` and
up would belong to that word.

The 25 known labels are carried in the register's `enum` map and surface as the
`label` field of a decoded reading.

---

## Other quirks worth knowing

| Item | Detail |
|---|---|
| `31016~31017` T-phase power | Documented as `S16`, but the address spans two registers and the R/S phases are `S32` at the same offset — so it is treated as `S32` here. |
| `32056` `battery_current` | Positive means **discharge**, not charge. |
| `32057~32058` `battery_power` | Positive means **discharge**. |
| `33016~33017` `meter_total_p` | Positive = generation, negative = consumption. |
| `30412~30416` `bms_software_version` | Depends on which battery is attached; may be absent. |
| `32240~32293` `battery2_parameters` | Second-battery block, added in v0.0.7 — only valid on inverters with two BMS interfaces. A dongle without the range in its table answers the bare `64 07 01` stub, which is **parked after the first probe**: the block stays listed in `_unreadable_ranges` (and its keys read `unavailable`) but is not re-read until `connect()` runs again. |
| `33101~33143` `meter2_parameters` | Second-meter block, added in v0.0.7 — only valid when a second meter is installed; expect the same refusal-or-zeros behaviour as battery2 on units without one, stubs parked the same way. |

---

## Next

- [Caveats & Troubleshooting](../caveats.md) — firmware differences and connection limits.
- [CLI Reference](../cli.md) — `--list`, `--section`, and `--key`.
