-- Author: Migration
-- Database: Neevo_InsightQuest2026_D
-- Execute through the Python runner, which owns the transaction.
-- The runner rolls back by default; --apply explicitly commits.
-- No source tables or business data are modified.

IF OBJECT_ID(N'dbo.Insight Project') IS NOT NULL
BEGIN
    IF OBJECT_ID(N'dbo.Insight Project', N'U') IS NULL
        THROW 51001, 'dbo.Insight Project exists but is not a table.', 1;

    IF (SELECT COUNT(*) FROM sys.columns
        WHERE object_id = OBJECT_ID(N'dbo.Insight Project')) <> 1
        OR NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID(N'dbo.Insight Project')
                AND name = N'Id'
                AND system_type_id = TYPE_ID(N'uniqueidentifier')
                AND is_nullable = 0
        )
        OR NOT EXISTS (
            SELECT 1
            FROM sys.indexes i
            JOIN sys.index_columns ic
                ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
                ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.object_id = OBJECT_ID(N'dbo.Insight Project')
                AND i.is_primary_key = 1 AND c.name = N'Id'
        )
        THROW 51002, 'Existing dbo.Insight Project does not match the initial GUID schema.', 1;
END
ELSE
BEGIN
    CREATE TABLE [dbo].[Insight Project] (
        [Id] UNIQUEIDENTIFIER NOT NULL
            CONSTRAINT [DF_InsightProject_Id] DEFAULT NEWID(),
        CONSTRAINT [PK_InsightProject] PRIMARY KEY ([Id])
    );
END;
