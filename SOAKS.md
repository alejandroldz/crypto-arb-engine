
## Soak v2 (codigo corregido)
- soak v1 (94bf456 congelado): 2026-11-13 17:05 -> 19:13 CET, solo colector
- soak v2 start: 2026-11-13 19:13 CET, HEAD 62c8099 (197 tests verdes, re-audit cerrado)
- 19:17 CET: historial de funding v1 (normalizacion pre-fix F02/F03) contaminaba la corroboracion #11 -> cuarentena permanente; funding_log.csv archivado y DBs reseteadas para soak limpio; v1 respaldado en soak-data
- 20:19 CET: reinicio en 2451d84 (fix raza DuckDB); hueco de datos 19:43-20:11 (motor muerto, causa desconocida: proceso reiniciado 20:10:53 sin intervencion; posible reciclaje del sandbox)
