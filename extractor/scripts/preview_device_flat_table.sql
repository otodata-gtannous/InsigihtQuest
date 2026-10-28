SELECT
    d.[Id],
    d.[SerialNumber],
    warranty.[WarrantyStartDate] AS [PurchaseDate],
    CAST(N'DeviceWarranties.StartDate (earliest nondeleted; proxy)'
        AS nvarchar(80)) AS [PurchaseDateSource],
    d.[Type] AS [DeviceType],
    tank.[TankInstallDate],
    d.[InitialInstallDate] AS [DeviceInitialInstallDate],
    owner_record.[CompanyId],
    company.[Name] AS [CompanyName],
    company.[TerritoryId],
    territory.[Name] AS [Territory],
    gps.[Longitude],
    gps.[Latitude],
    gps.[Longitude] AS [DeviceLong],
    gps.[Latitude] AS [DeviceLat],
    gps.[LastGpsDate],
    d.[CreatedDate] AS [DeviceCreatedDate],
    d.[IsActive] AS [DeviceIsActive],
    d.[IsDeleted] AS [DeviceIsDeleted],
    company.[IsActive] AS [CompanyIsActive],
    company.[IsDeleted] AS [CompanyIsDeleted],
    owner_count.[OwnershipCandidateCount],
    tank.[TankCount],
    warranty.[WarrantyStartDate],
    warranty.[WarrantyCount],
    COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) AS [AnalysisInstalledAt]
FROM [dbo].[Devices] d
LEFT JOIN [dbo].[DeviceLocations] location ON location.[Id] = d.[Id]
OUTER APPLY (
    -- Keep coordinates and their timestamp from the same source.
    SELECT
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[Latitude] ELSE d.[Latitude] END AS [Latitude],
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[Longitude] ELSE d.[Longitude] END AS [Longitude],
        CASE WHEN location.[Latitude] IS NOT NULL AND location.[Longitude] IS NOT NULL
            THEN location.[LastGpsDate] ELSE d.[LastGpsDate] END AS [LastGpsDate]
) gps
OUTER APPLY (
    SELECT TOP (1) o.[CompanyId]
    FROM [dbo].[Ownerships] o
    WHERE o.[DeviceId] = d.[Id]
        AND o.[IsOwner] = 1 AND o.[IsActive] = 1 AND o.[IsDeleted] = 0
    ORDER BY o.[CreatedDate] DESC, o.[Id] DESC
) owner_record
OUTER APPLY (
    SELECT COUNT_BIG(*) AS [OwnershipCandidateCount]
    FROM [dbo].[Ownerships] o
    WHERE o.[DeviceId] = d.[Id]
        AND o.[IsOwner] = 1 AND o.[IsActive] = 1 AND o.[IsDeleted] = 0
) owner_count
LEFT JOIN [dbo].[Companies] company ON company.[Id] = owner_record.[CompanyId]
LEFT JOIN [dbo].[Territories] territory ON territory.[Id] = company.[TerritoryId]
OUTER APPLY (
    SELECT MIN(w.[StartDate]) AS [WarrantyStartDate], COUNT_BIG(*) AS [WarrantyCount]
    FROM [dbo].[DeviceWarranties] w
    WHERE w.[DeviceId] = d.[Id] AND w.[IsDeleted] = 0
) warranty
OUTER APPLY (
    SELECT MIN(t.[InstalledDate]) AS [TankInstallDate],
        COUNT(DISTINCT t.[Id]) AS [TankCount]
    FROM [dbo].[Sensors] s
    JOIN [dbo].[TankSensors] ts ON ts.[SensorId] = s.[Id]
    JOIN [dbo].[Tanks] t ON t.[Id] = ts.[TankId]
    WHERE s.[DeviceId] = d.[Id]
        AND s.[IsDeleted] = 0 AND ts.[IsDeleted] = 0 AND t.[IsDeleted] = 0
) tank;
