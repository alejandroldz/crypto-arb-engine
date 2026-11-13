
## Soak v2 (codigo corregido)
- soak v1 (94bf456 congelado): 2026-11-13 17:05 -> 19:13 CET, solo colector
- soak v2 start: 2026-11-13 19:13 CET, HEAD 62c8099 (197 tests verdes, re-audit cerrado)
- 19:17 CET: historial de funding v1 (normalizacion pre-fix F02/F03) contaminaba la corroboracion #11 -> cuarentena permanente; funding_log.csv archivado y DBs reseteadas para soak limpio; v1 respaldado en soak-data
- 20:19 CET: reinicio en 2451d84 (fix raza DuckDB); hueco de datos 19:43-20:11 (motor muerto, causa desconocida: proceso reiniciado 20:10:53 sin intervencion; posible reciclaje del sandbox)
- 21:48 CET: fix ventana historica Kraken (9e46e0d), reinicio del soak; la cuarentena ETH era falsa (media de nov 2025)
- NOTA: tras el fix, la corroboracion sigue cuarentenando kf ETH (media 30d +0.0000071 vs polled reciente -0.0000078): es el check siendo conservador (ventana 30d vs historia fresca de horas, funding negativo hoy). Fail-closed correcto; revisar si persiste dias
