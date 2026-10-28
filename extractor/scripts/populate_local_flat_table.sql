:On Error exit
-- Run in SQLCMD mode from extractor\scripts with -v Apply=0 (rehearse) or Apply=1.
SET NOCOUNT ON;
SET XACT_ABORT ON;
IF DB_NAME() <> N'otodatawebportal_dev'
    THROW 51100, 'This script is restricted to otodatawebportal_dev.', 1;
IF N'$(Apply)' NOT IN (N'0', N'1')
    THROW 51101, 'Apply must be 0 (rollback) or 1 (commit).', 1;

BEGIN TRY
    BEGIN TRANSACTION;
    DECLARE @lockResult int;
    EXEC @lockResult = sys.sp_getapplock
        @Resource = N'InsightQuestLocalFlatTable',
        @LockMode = N'Exclusive', @LockOwner = N'Transaction', @LockTimeout = 10000;
    IF @lockResult < 0
        THROW 51102, 'Could not acquire the local flat-table refresh lock.', 1;

    CREATE TABLE #DeviceFlatSource (
        [Id] uniqueidentifier NOT NULL PRIMARY KEY,
        [SerialNumber] bigint NOT NULL,
        [PurchaseDate] date NULL,
        [PurchaseDateSource] nvarchar(80) NOT NULL,
        [DeviceType] smallint NOT NULL,
        [TankInstallDate] datetime NULL,
        [DeviceInitialInstallDate] datetime NULL,
        [CompanyId] uniqueidentifier NULL,
        [CompanyName] nvarchar(128) NULL,
        [TerritoryId] uniqueidentifier NULL,
        [Territory] nvarchar(128) NULL,
        [Longitude] decimal(9,6) NULL,
        [Latitude] decimal(9,6) NULL,
        [DeviceLong] decimal(9,6) NULL,
        [DeviceLat] decimal(9,6) NULL,
        [LastGpsDate] datetime NULL,
        [DeviceCreatedDate] datetime NOT NULL,
        [DeviceIsActive] bit NOT NULL,
        [DeviceIsDeleted] bit NOT NULL,
        [CompanyIsActive] bit NULL,
        [CompanyIsDeleted] bit NULL,
        [OwnershipCandidateCount] bigint NOT NULL,
        [TankCount] int NOT NULL,
        [WarrantyStartDate] date NULL,
        [WarrantyCount] bigint NOT NULL,
        [AnalysisInstalledAt] datetime NULL
    );
    INSERT INTO #DeviceFlatSource
:r .\preview_device_flat_table.sql

    IF NOT EXISTS (SELECT 1 FROM #DeviceFlatSource)
        THROW 51103, 'No source devices found; refusing to empty the target.', 1;

    IF OBJECT_ID(N'dbo.Insight Project') IS NULL
    BEGIN
        SELECT * INTO [dbo].[Insight Project] FROM #DeviceFlatSource WHERE 1 = 0;
        ALTER TABLE [dbo].[Insight Project] ADD PRIMARY KEY ([Id]);
    END;
    ELSE IF OBJECT_ID(N'dbo.Insight Project', N'U') IS NULL
        THROW 51104, 'dbo.Insight Project exists but is not a table.', 1;

    -- Refuse legacy or unexpected schemas instead of dropping/replacing them.
    IF EXISTS (
        SELECT [name], [system_type_id], [max_length], [precision], [scale], [is_nullable]
        FROM sys.columns WHERE [object_id] = OBJECT_ID(N'dbo.Insight Project')
        EXCEPT
        SELECT [name], [system_type_id], [max_length], [precision], [scale], [is_nullable]
        FROM tempdb.sys.columns WHERE [object_id] = OBJECT_ID(N'tempdb..#DeviceFlatSource')
    ) OR EXISTS (
        SELECT [name], [system_type_id], [max_length], [precision], [scale], [is_nullable]
        FROM tempdb.sys.columns WHERE [object_id] = OBJECT_ID(N'tempdb..#DeviceFlatSource')
        EXCEPT
        SELECT [name], [system_type_id], [max_length], [precision], [scale], [is_nullable]
        FROM sys.columns WHERE [object_id] = OBJECT_ID(N'dbo.Insight Project')
    )
        THROW 51105, 'Target schema differs; review it manually before refreshing.', 1;

    DECLARE @before bigint = (SELECT COUNT_BIG(*) FROM [dbo].[Insight Project]);
    DELETE FROM [dbo].[Insight Project];
    INSERT INTO [dbo].[Insight Project] (
        [Id], [SerialNumber], [PurchaseDate], [PurchaseDateSource], [DeviceType],
        [TankInstallDate], [DeviceInitialInstallDate], [CompanyId], [CompanyName],
        [TerritoryId], [Territory], [Longitude], [Latitude], [DeviceLong], [DeviceLat],
        [LastGpsDate], [DeviceCreatedDate], [DeviceIsActive], [DeviceIsDeleted],
        [CompanyIsActive], [CompanyIsDeleted], [OwnershipCandidateCount], [TankCount],
        [WarrantyStartDate], [WarrantyCount], [AnalysisInstalledAt]
    )
    SELECT * FROM #DeviceFlatSource;

    DECLARE @after bigint = (SELECT COUNT_BIG(*) FROM [dbo].[Insight Project]);
    IF @after <> (SELECT COUNT_BIG(*) FROM #DeviceFlatSource)
        THROW 51106, 'Target row count does not match the source.', 1;

    SELECT @before AS [PreviousRows], @after AS [RefreshedRows],
        SUM(CASE WHEN [OwnershipCandidateCount] > 1 THEN 1 ELSE 0 END) AS [MultipleOwnerDevices],
        SUM(CASE WHEN [TankCount] > 1 THEN 1 ELSE 0 END) AS [MultipleTankDevices],
        SUM(CASE WHEN [PurchaseDate] IS NULL THEN 1 ELSE 0 END) AS [MissingPurchaseProxy],
        SUM(CASE WHEN [CompanyId] IS NULL THEN 1 ELSE 0 END) AS [MissingCompany]
    FROM #DeviceFlatSource;

    DROP TABLE #DeviceFlatSource;
    IF N'$(Apply)' = N'1'
    BEGIN
        COMMIT TRANSACTION;
        PRINT 'Local flat-table refresh committed.';
    END;
    ELSE
    BEGIN
        ROLLBACK TRANSACTION;
        PRINT 'Rehearsal rolled back. No flat-table changes persisted.';
    END;
END TRY
BEGIN CATCH
    IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
    THROW;
END CATCH;
