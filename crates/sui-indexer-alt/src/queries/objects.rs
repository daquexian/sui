// Copyright (c) Mysten Labs, Inc.
// SPDX-License-Identifier: Apache-2.0

use diesel::dsl::sql_query;
use diesel::{
    sql_types::{BigInt, Bytea},
    QueryableByName, Selectable,
};
use diesel_async::RunQueryDsl;
use move_core_types::language_storage::StructTag;
use sui_indexer_alt_schema::objects::{StoredObjVersion, StoredOwnerKind};
use sui_indexer_alt_schema::schema::obj_info;
use sui_pg_db::Db;
use sui_types::base_types::{ObjectID, SuiAddress};

pub struct Cursor {
    checkpoint: i64,
    object_id: ObjectID,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ModuleFilter {
    /// Filter the module by the package it's from.
    ByPackage(SuiAddress),

    /// Exact match on the module.
    ByModule(SuiAddress, String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TypeFilter {
    /// Filter the type by the package or module it's from.
    ByModule(ModuleFilter),

    /// If the struct tag has type parameters, treat it as an exact filter on that instantiation,
    /// otherwise treat it as either a filter on all generic instantiations of the type, or an exact
    /// match on the type with no type parameters. E.g.
    ///
    ///  0x2::coin::Coin
    ///
    /// would match both 0x2::coin::Coin and 0x2::coin::Coin<0x2::sui::SUI>.
    ByType(StructTag),
}

#[derive(Default, Debug, Clone, Eq, PartialEq)]
pub struct ObjectFilter {
    /// Filter objects by their type's `package`, `package::module`, or their fully qualified type
    /// name.
    ///
    /// Generic types can be queried by either the generic type name, e.g. `0x2::coin::Coin`, or by
    /// the full type name, such as `0x2::coin::Coin<0x2::sui::SUI>`.
    pub type_filter: Option<TypeFilter>,

    /// Filter for live objects by their current owners.
    pub owner_filter: Option<SuiAddress>,
}

#[derive(QueryableByName, Selectable, Debug)]
#[diesel(table_name = obj_info)]
struct IdCheckpoint {
    #[diesel(sql_type = BigInt)]
    cp_sequence_number: i64,
    #[diesel(sql_type = Bytea)]
    object_id: Vec<u8>,
}

pub async fn query_objects_with_filters(
    db: &Db,
    view_checkpoint_number: i64,
    filters: ObjectFilter,
    cursor: Option<Cursor>,
    limit: usize,
) -> anyhow::Result<Vec<StoredObjVersion>> {
    let object_ids =
        query_object_ids_with_filters(db, view_checkpoint_number, filters, cursor, limit).await?;
    query_latest_object_versions(db, &object_ids).await
}

// TODO: Double check that this function is not prone to SQL injection.
async fn query_object_ids_with_filters(
    db: &Db,
    view_checkpoint_number: i64,
    filters: ObjectFilter,
    cursor: Option<Cursor>,
    limit: usize,
) -> anyhow::Result<Vec<IdCheckpoint>> {
    let mut filter_conditions = vec![];
    if let Some(owner) = filters.owner_filter {
        filter_conditions.push(format!("owner_kind = {}", StoredOwnerKind::Address as i16));
        filter_conditions.push(format!(
            "owner_id = '\\x{}'::bytea",
            hex::encode(owner.to_vec())
        ));
    }

    if let Some(type_filter) = filters.type_filter {
        match type_filter {
            TypeFilter::ByModule(module) => match module {
                ModuleFilter::ByPackage(package) => {
                    filter_conditions.push(format!(
                        "package = '\\x{}'::bytea",
                        hex::encode(package.to_vec())
                    ));
                }
                ModuleFilter::ByModule(package, module) => {
                    filter_conditions.push(format!(
                        "package = '\\x{}'::bytea",
                        hex::encode(package.to_vec()),
                    ));
                    filter_conditions.push(format!("module = '{}'", module));
                }
            },
            TypeFilter::ByType(struct_tag) => {
                filter_conditions.push(format!(
                    "package = '\\x{}'::bytea",
                    hex::encode(struct_tag.address.to_vec())
                ));
                filter_conditions.push(format!("module = '{:?}'", struct_tag.module.as_str()));
                filter_conditions.push(format!("name = '{:?}'", struct_tag.name.as_str()));
                filter_conditions.push(format!(
                    "instantiation = '\\x{}'::bytea",
                    hex::encode(bcs::to_bytes(&struct_tag.type_params).unwrap())
                ));
            }
        }
    }

    filter_conditions.push(format!("cp_sequence_number <= {view_checkpoint_number}"));

    if let Some(cursor) = cursor {
        filter_conditions.push(format!(
            "(cp_sequence_number < {} OR (cp_sequence_number = {} AND object_id > '\\x{}'::bytea))",
            cursor.checkpoint,
            cursor.checkpoint,
            hex::encode(cursor.object_id)
        ));
    }
    let filter_conditions_str = filter_conditions.join(" AND ");

    let query = format!(
        "
        WITH filtered_rows AS (
            SELECT
                cp_sequence_number,
                object_id
            FROM
                obj_info
            WHERE
                {filter_conditions_str}
        )
        SELECT
            f.cp_sequence_number,
            f.object_id
        FROM
            filtered_rows f
        LEFT JOIN
            obj_info o
        ON
            f.object_id = o.object_id
            AND o.cp_sequence_number > f.cp_sequence_number
            AND o.cp_sequence_number <= {view_checkpoint_number}
        WHERE
            o.object_id IS NULL
        ORDER BY
            f.cp_sequence_number DESC,
            f.object_id ASC
        LIMIT {limit};
        ",
    );

    println!("{}", query);
    let sql_query = sql_query(query);
    let mut conn = db.connect().await?;
    Ok(sql_query.load::<IdCheckpoint>(&mut conn).await?)
}

async fn query_latest_object_versions(
    db: &Db,
    objects: &[IdCheckpoint],
) -> anyhow::Result<Vec<StoredObjVersion>> {
    if objects.is_empty() {
        return Ok(vec![]);
    }
    let conditions = objects
        .iter()
        .map(|o| {
            format!(
                "(object_id = '\\x{}'::bytea AND cp_sequence_number <= {})",
                hex::encode(&o.object_id),
                o.cp_sequence_number
            )
        })
        .collect::<Vec<_>>()
        .join(" OR ");
    let query = format!(
        "
        SELECT obj_versions.*
        FROM obj_versions
        JOIN (
            SELECT object_id, MAX(cp_sequence_number) AS max_cp_sequence_number
            FROM obj_versions
            WHERE {}
            GROUP BY object_id
        ) AS filtered_objects
        ON obj_versions.object_id = filtered_objects.object_id
        AND obj_versions.cp_sequence_number = filtered_objects.max_cp_sequence_number
        ",
        conditions
    );
    println!("{}", query);
    let sql_query = sql_query(query);
    let mut conn = db.connect().await?;
    Ok(sql_query.load::<StoredObjVersion>(&mut conn).await?)
}

#[cfg(test)]
mod tests {
    use move_core_types::ident_str;
    use sui_indexer_alt_framework::Indexer;
    use sui_indexer_alt_schema::MIGRATIONS;

    use super::*;

    #[tokio::test]
    async fn test_query_objects_with_filters() {
        let (indexer, _db) = Indexer::new_for_testing(&MIGRATIONS).await;
        let mut conn = indexer.db().connect().await.unwrap();
        query_objects_with_filters(
            &indexer.db(),
            1000,
            ObjectFilter {
                type_filter: Some(TypeFilter::ByType(StructTag {
                    address: SuiAddress::ZERO.into(),
                    module: ident_str!("coin").to_owned(),
                    name: ident_str!("Coin").to_owned(),
                    type_params: vec![],
                })),
                owner_filter: Some(SuiAddress::ZERO),
            },
            None,
            100,
        )
        .await
        .unwrap();
    }
}
