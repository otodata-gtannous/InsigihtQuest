-- Set-based equivalent of preview_device_flat_table.sql for the full snapshot.
WITH OwnerCandidates AS (
    SELECT o.[DeviceId], o.[CompanyId],
        COUNT_BIG(*) OVER (PARTITION BY o.[DeviceId]) AS [OwnershipCandidateCount],
        ROW_NUMBER() OVER (
            PARTITION BY o.[DeviceId] ORDER BY o.[CreatedDate] DESC, o.[Id] DESC
        ) AS [OwnerRank]
    FROM [dbo].[Ownerships] o
    WHERE o.[IsOwner] = 1 AND o.[IsActive] = 1 AND o.[IsDeleted] = 0
),
Warranties AS (
    SELECT w.[DeviceId], MIN(w.[StartDate]) AS [WarrantyStartDate],
        COUNT_BIG(*) AS [WarrantyCount]
    FROM [dbo].[DeviceWarranties] w
    WHERE w.[IsDeleted] = 0
    GROUP BY w.[DeviceId]
),
DeviceTanks AS (
    SELECT s.[DeviceId], t.[Id] AS [TankId], t.[InstalledDate]
    FROM [dbo].[Sensors] s
    JOIN [dbo].[TankSensors] ts ON ts.[SensorId] = s.[Id]
    JOIN [dbo].[Tanks] t ON t.[Id] = ts.[TankId]
    WHERE s.[IsDeleted] = 0 AND ts.[IsDeleted] = 0 AND t.[IsDeleted] = 0
    GROUP BY s.[DeviceId], t.[Id], t.[InstalledDate]
),
TankDates AS (
    SELECT [DeviceId], MIN([InstalledDate]) AS [TankInstallDate],
        COUNT(*) AS [TankCount]
    FROM DeviceTanks
    GROUP BY [DeviceId]
)
SELECT
    d.[Id], d.[SerialNumber],
    warranty.[WarrantyStartDate] AS [PurchaseDate],
    CAST(N'DeviceWarranties.StartDate (earliest nondeleted; proxy)'
        AS nvarchar(80)) AS [PurchaseDateSource],
    d.[Type] AS [DeviceType], tank.[TankInstallDate],
    d.[InitialInstallDate] AS [DeviceInitialInstallDate],
    owner_record.[CompanyId], company.[Name] AS [CompanyName],
    company.[TerritoryId], territory.[Name] AS [Territory],
    gps.[Longitude], gps.[Latitude],
    gps.[Longitude] AS [DeviceLong], gps.[Latitude] AS [DeviceLat],
    gps.[LastGpsDate], d.[CreatedDate] AS [DeviceCreatedDate],
    d.[IsActive] AS [DeviceIsActive], d.[IsDeleted] AS [DeviceIsDeleted],
    company.[IsActive] AS [CompanyIsActive], company.[IsDeleted] AS [CompanyIsDeleted],
    COALESCE(owner_record.[OwnershipCandidateCount], CONVERT(bigint, 0)) AS [OwnershipCandidateCount],
    COALESCE(tank.[TankCount], 0) AS [TankCount],
    warranty.[WarrantyStartDate],
    COALESCE(warranty.[WarrantyCount], CONVERT(bigint, 0)) AS [WarrantyCount],
    COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) AS [AnalysisInstalledAt]
INTO #DeviceStage
FROM [dbo].[Devices] d
JOIN #DeviceIds captured ON captured.[Id] = d.[Id]
LEFT JOIN [dbo].[DeviceLocations] location ON location.[Id] = d.[Id]
OUTER APPLY (
    SELECT
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[Latitude] ELSE d.[Latitude] END AS [Latitude],
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[Longitude] ELSE d.[Longitude] END AS [Longitude],
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[LastGpsDate] ELSE d.[LastGpsDate] END AS [LastGpsDate]
) gps
LEFT JOIN OwnerCandidates owner_record ON owner_record.[DeviceId] = d.[Id]
    AND owner_record.[OwnerRank] = 1
LEFT JOIN [dbo].[Companies] company ON company.[Id] = owner_record.[CompanyId]
LEFT JOIN [dbo].[Territories] territory ON territory.[Id] = company.[TerritoryId]
LEFT JOIN Warranties warranty ON warranty.[DeviceId] = d.[Id]
LEFT JOIN TankDates tank ON tank.[DeviceId] = d.[Id];
