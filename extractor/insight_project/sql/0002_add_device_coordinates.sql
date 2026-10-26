-- Author: Migration
-- Target: Neevo_InsightQuest2026_D, dbo.Insight Project only.
-- Run through Python: migrate rehearses and rolls back; migrate --apply commits.
-- Source coordinates are nullable decimal(9,6); preserve their exact values.
ALTER TABLE [dbo].[Insight Project]
ADD [DeviceLong] DECIMAL(9,6) NULL,
    [DeviceLat] DECIMAL(9,6) NULL;
