-- Schema upgrade only. Review missing migration files 0003-0005 before deployment.
-- Run in a dedicated session. Change @Apply to 1 only after deployment approval.
SET NOCOUNT ON;
SET XACT_ABORT ON;
SET LOCK_TIMEOUT 5000;
DECLARE @Apply bit = 0;
IF DB_NAME() <> N'Neevo_InsightQuest2026_D'
    THROW 51200, 'Wrong database for the Azure upgrade.', 1;
IF @@TRANCOUNT <> 0
    THROW 51201, 'Use a dedicated session without an existing transaction.', 1;
IF OBJECT_ID(N'dbo.Insight Project', N'U') IS NULL
    THROW 51202, 'Expected existing Insight Project table.', 1;

BEGIN TRY
    BEGIN TRANSACTION;
    DECLARE @result int;
    EXEC @result = sys.sp_getapplock
        @Resource = N'InsightProjectMigrations', @LockMode = N'Exclusive',
        @LockOwner = N'Transaction', @LockTimeout = 5000;
    IF @result < 0 THROW 51203, 'Could not acquire migration lock.', 1;

    -- INT coordinates have already lost precision; subsequent backfill must use source decimals.
    ALTER TABLE [dbo].[Insight Project] ALTER COLUMN [Longitude] decimal(9,6) NULL;
    ALTER TABLE [dbo].[Insight Project] ALTER COLUMN [Latitude] decimal(9,6) NULL;

    DECLARE @columns TABLE ([Name] sysname, [Definition] nvarchar(100));
    INSERT INTO @columns VALUES
        (N'SerialNumber', N'bigint NULL'),
        (N'PurchaseDateSource', N'nvarchar(80) NULL'),
        (N'CompanyName', N'nvarchar(128) NULL'),
        (N'TerritoryId', N'uniqueidentifier NULL'),
        (N'Territory', N'nvarchar(128) NULL'),
        (N'DeviceLong', N'decimal(9,6) NULL'),
        (N'DeviceLat', N'decimal(9,6) NULL'),
        (N'LastGpsDate', N'datetime NULL'),
        (N'DeviceCreatedDate', N'datetime NULL'),
        (N'DeviceIsActive', N'bit NULL'),
        (N'DeviceIsDeleted', N'bit NULL'),
        (N'CompanyIsActive', N'bit NULL'),
        (N'CompanyIsDeleted', N'bit NULL'),
        (N'OwnershipCandidateCount', N'bigint NULL'),
        (N'TankCount', N'int NULL'),
        (N'WarrantyStartDate', N'date NULL'),
        (N'WarrantyCount', N'bigint NULL'),
        (N'AnalysisInstalledAt', N'datetime NULL');
    DECLARE @name sysname, @definition nvarchar(100), @sql nvarchar(max);
    DECLARE additions CURSOR LOCAL FAST_FORWARD FOR SELECT [Name], [Definition] FROM @columns;
    OPEN additions;
    FETCH NEXT FROM additions INTO @name, @definition;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        IF COL_LENGTH(N'dbo.Insight Project', @name) IS NULL
        BEGIN
            SET @sql = N'ALTER TABLE [dbo].[Insight Project] ADD '
                + QUOTENAME(@name) + N' ' + @definition + N';';
            EXEC sys.sp_executesql @sql;
        END;
        FETCH NEXT FROM additions INTO @name, @definition;
    END;
    CLOSE additions;
    DEALLOCATE additions;

    IF @Apply = 1
    BEGIN
        COMMIT TRANSACTION;
        PRINT 'Schema upgrade committed; data backfill is still required.';
    END;
    ELSE
    BEGIN
        ROLLBACK TRANSACTION;
        PRINT 'Schema upgrade rehearsal rolled back.';
    END;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
