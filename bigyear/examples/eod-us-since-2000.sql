SELECT
  "year",
  "month",
  "day",
  stateprovince,
  v_scientificname,
  v_taxonconceptid,
  decimallatitude,
  decimallongitude,
  MIN(gbifid) AS representative_gbifid,
  COUNT(*) AS source_record_count,
  SUM(CASE WHEN hasgeospatialissues = TRUE THEN 1 ELSE 0 END) AS geospatial_flagged_records,
  SUM(CASE WHEN hasgeospatialissues IS NULL THEN 1 ELSE 0 END) AS geospatial_unknown_records,
  MAX(coordinateuncertaintyinmeters) AS max_reported_coordinate_uncertainty_m,
  COUNT(coordinateuncertaintyinmeters) AS records_with_reported_coordinate_uncertainty
FROM occurrence
WHERE datasetkey = '4fa7b334-ce0d-4e88-aaae-2e0c138d049e'
  AND countrycode = 'US'
  AND "class" = 'Aves'
  AND basisofrecord = 'HUMAN_OBSERVATION'
  AND "year" >= 2000
  AND hascoordinate = TRUE
  AND "month" IS NOT NULL
  AND "day" IS NOT NULL
  AND occurrencestatus = 'PRESENT'
GROUP BY "year", "month", "day", stateprovince, v_scientificname, v_taxonconceptid, decimallatitude, decimallongitude
