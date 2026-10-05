Machines (machine id: description):
- pump-1: Cooling water pump, end-suction centrifugal, 7.5 kW, 2900 rpm. Duty pump.
- pump-2: Process water pump, end-suction centrifugal, 11 kW, 2900 rpm. Duty pump. Its drive-end bearing failed on {date:62}: vibration rose from 2.3 to 7.0 mm/s over three weeks, with bearing temperature climbing to 68 °C, after a missed re-greasing. Bearing replaced. Details in archival memory.
- fan-1: Supply air fan, belt driven, 5.5 kW, 1450 rpm.

Sensors (topic sensors/<machine id>/<sensor>):
- vibration: velocity RMS on the drive-end bearing housing, mm/s
- bearing_temp: drive-end bearing temperature, °C
- current: motor current, A

Vibration zones (ISO 10816-3, machines 15 kW or less on rigid foundations):
- up to 2.8 mm/s: good
- 2.8 to 4.5 mm/s: acceptable for long-term operation; watch the trend
- 4.5 to 7.1 mm/s: unsatisfactory; plan maintenance soon
- above 7.1 mm/s: unacceptable; risk of damage, act now
A steady rise is a warning even inside a zone. Bearing temperature above 70 °C, or 15 °C above its usual level, points to lubrication or bearing problems.

Maintenance plan:
- Bearings re-greased every 2000 running hours (about 12 weeks on duty pumps).
