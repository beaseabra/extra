# ===== framework_metadata_executor.py =====
# AiBizCore — Framework Metadata Executor
#
# Objetivo:
# - Executar a criação da metadata da framework AiBizCore/ecobite.
# - Usa o plano gerado por framework_object_planner.py.
# - Usa o preflight de framework_metadata_preflight.py antes de escrever.
#
# Segurança:
# - Por defeito NÃO executa.
# - Execução real exige:
#     1) AIBIZCORE_ENABLE_FRAMEWORK_EXECUTION=true no .env
#     2) execute=True
#     3) confirm_phrase="EXECUTE_FRAMEWORK_METADATA"
#     4) preflight can_execute=True
#
# Regra crítica:
# - CSYSObject.nameunc é exatamente o nome da tabela física.
# - CSYSObject.pkfieldname é exatamente a primary key física.
# - CSYSObjectField.nameunc / CSYSObjectReference.nameunc são exatamente colunas físicas.
#
# Uso CLI dry-run:
#   python3 framework_metadata_executor.py blueprint.json
#
# Uso CLI execute:
#   python3 framework_metadata_executor.py blueprint.json --execute EXECUTE_FRAMEWORK_METADATA

from __future__ import annotations

import json
import re
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from framework_metadata_preflight import (
    connect_pymssql,
    load_local_env,
    run_framework_metadata_preflight,
)


ENABLE_ENV = "AIBIZCORE_ENABLE_FRAMEWORK_EXECUTION"
CONFIRM_PHRASE = "EXECUTE_FRAMEWORK_METADATA"

_TABLE_COLUMN_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


# ---------------------------------------------------------------------
# Configuração / segurança
# ---------------------------------------------------------------------

def is_framework_execution_enabled() -> bool:
    load_local_env(".env")
    return str(os.getenv(ENABLE_ENV, "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_guid() -> str:
    return str(uuid.uuid4()).upper()


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    value = value.strip()

    # Só placeholders técnicos do planner devem ser resolvidos.
    # Antes, qualquer XML/HTML que começasse por "<", terminasse por ">" e
    # tivesse ":" algures era convertido para GUID. Isso corrompia:
    # - CSYSObjectLayoutSection.workingdata
    # - CSYSView.clientdata
    return re.fullmatch(r"<[A-Z0-9_]+:[^<>]+>", value) is not None


def _resolve_placeholders(value: Any, placeholder_map: Dict[str, str]) -> Any:
    """
    Substitui placeholders do tipo <OBJECT_ID:...> por GUIDs reais.
    Mantém strings normais inalteradas.
    """
    if isinstance(value, dict):
        return {
            key: _resolve_placeholders(item, placeholder_map)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _resolve_placeholders(item, placeholder_map)
            for item in value
        ]

    if value == "<NOW>":
        return _now_utc().replace(tzinfo=None)

    if _is_placeholder(value):
        key = value.strip()
        if key not in placeholder_map:
            placeholder_map[key] = _new_guid()
        return placeholder_map[key]

    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}

    if isinstance(value, list):
        return [_json_safe(item) for item in value]

    return value


# ---------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------

def _execute(cursor, sql: str, params: Sequence[Any]) -> None:
    cursor.execute(sql, tuple(params))


def _load_table_columns(cursor, table: str) -> Dict[str, Dict[str, Any]]:
    """
    Lê metadata da tabela para evitar erros de INSERT por colunas NOT NULL
    não enviadas explicitamente.

    Isto é importante porque algumas tabelas CSYS têm campos BIT obrigatórios
    sem default visível, por exemplo CSYSObjectField.isvirtual.
    """
    cache_key = table.lower()
    if cache_key in _TABLE_COLUMN_CACHE:
        return _TABLE_COLUMN_CACHE[cache_key]

    cursor.execute(
        """
        SELECT
            COLUMN_NAME,
            DATA_TYPE,
            IS_NULLABLE,
            COLUMN_DEFAULT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = %s
        """,
        (table,),
    )

    columns: Dict[str, Dict[str, Any]] = {}
    for column_name, data_type, is_nullable, column_default in cursor.fetchall():
        columns[str(column_name)] = {
            "data_type": str(data_type or "").lower(),
            "is_nullable": str(is_nullable or "").upper() == "YES",
            "column_default": column_default,
        }

    _TABLE_COLUMN_CACHE[cache_key] = columns
    return columns


def _default_for_required_column(column: str, data_type: str) -> Any:
    """
    Default conservador para colunas NOT NULL sem default.
    Só é usado quando a coluna não foi enviada pelo plano.
    """
    data_type = str(data_type or "").lower()

    if data_type == "bit":
        return 0

    if data_type in {"int", "bigint", "smallint", "tinyint"}:
        return 0

    if data_type in {"decimal", "numeric", "money", "smallmoney", "float", "real"}:
        return 0

    if data_type in {"datetime", "datetime2", "datetimeoffset", "smalldatetime", "date", "time"}:
        return _now_utc().replace(tzinfo=None)

    if data_type == "uniqueidentifier":
        return _new_guid()

    if data_type == "xml":
        return "<data/>"

    if data_type in {"nvarchar", "varchar", "nchar", "char", "text", "ntext"}:
        return ""

    # Fallback. Se o SQL Server recusar, o erro continua explícito e a transação faz rollback.
    return ""


def _prepare_insert_data(cursor, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
    table_columns = _load_table_columns(cursor, table)

    # Remove campos que não existem fisicamente na tabela.
    prepared = {
        key: value
        for key, value in data.items()
        if key in table_columns
    }

    # Preenche colunas NOT NULL sem default que não estejam no plano.
    for column, meta in table_columns.items():
        if column in prepared:
            continue

        if meta["is_nullable"]:
            continue

        if meta.get("column_default") is not None:
            continue

        prepared[column] = _default_for_required_column(column, meta["data_type"])

    return prepared


def _insert(cursor, table: str, data: Dict[str, Any]) -> None:
    prepared = _prepare_insert_data(cursor, table, data)

    columns = list(prepared.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(f"[{column}]" for column in columns)

    sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
    params = [prepared[column] for column in columns]
    _execute(cursor, sql, params)


def _base_status_fields(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = {
        "creationdate": _now_utc(),
        "modificationdate": _now_utc(),
        "status": "Pub",
        "state": "Active",
        "ostate": "Active",
        "locked": 0,
        "descendantlocked": 0,
        "issystem": 0,
    }

    if data:
        base.update(data)

    return base


# ---------------------------------------------------------------------
# Inserts por tabela
# ---------------------------------------------------------------------

def insert_csys_object(cursor, data: Dict[str, Any]) -> None:
    # CSYSObject tem vários campos BIT sem NULL permitido na framework.
    # No processo manual estes campos ficam com valores neutros/false.
    # Se não forem enviados, SQL Server rejeita o INSERT, por exemplo:
    # "Cannot insert the value NULL into column 'hascustomercontrol'".
    row = _base_status_fields(
        {
            "objectid": data["objectid"],
            "name": data["name"],
            "description": data.get("description"),

            # Estado/base
            "status": data.get("status", "Pub"),
            "state": data.get("state", "Active"),
            "ostate": data.get("ostate", "Active"),
            "issystem": data.get("issystem", 0),
            "ishiden": data.get("ishiden", 0),
            "nameunc": data["nameunc"],
            "databasenameunc": data.get("databasenameunc"),
            "pkfieldname": data["pkfieldname"],
            "parentfieldname": data.get("parentfieldname"),
            "type": data.get("type"),
            "parentobjectid": data.get("parentobjectid"),

            # Segurança e controlo
            "securityuseparentworflowstate": data.get("securityuseparentworflowstate", 0),
            "securitytype": data.get("securitytype"),
            "customersecuritytype": data.get("customersecuritytype"),
            "externalsecuritytype": data.get("externalsecuritytype"),
            "workflowselectionscript": data.get("workflowselectionscript"),
            "serieselectionscript": data.get("serieselectionscript"),
            "hascustomercontrol": data.get("hascustomercontrol", 0),
            "hasexternalcontrol": data.get("hasexternalcontrol", 0),

            # Logs
            "hasLog": data.get("hasLog", 0),
            "logdelete": data.get("logdelete", 0),
            "logview": data.get("logview", 0),

            # Campos/configuração
            "forcenewfields": data.get("forcenewfields", 0),
            "userconfiguration": data.get("userconfiguration"),
            "workingdata": data.get("workingdata"),
            "serverdata": data.get("serverdata"),
            "clientdata": data.get("clientdata"),
            "lastcompiledate": data.get("lastcompiledate"),
            "lastcompileuserid": data.get("lastcompileuserid"),
            "iscompile": data.get("iscompile", 0),
            "ispublished": data.get("ispublished", 0),
            "titleformula": data.get("titleformula"),
            "defaultvalue": data.get("defaultvalue"),
            "formvalidation": data.get("formvalidation"),
            "clientjsscript": data.get("clientjsscript"),
            "clientcss": data.get("clientcss"),
            "serversqlscript": data.get("serversqlscript"),
            "iconresourceunc": data.get("iconresourceunc"),
            "command": data.get("command"),
            "paramoptions": data.get("paramoptions"),
            "hasreferencekeymapping": data.get("hasreferencekeymapping", 0),
            "baseapplicationid": data.get("baseapplicationid"),
            "hidemenu": data.get("hidemenu", 0),
            "extendedbaseobjectid": data.get("extendedbaseobjectid"),
            "hastranslationdata": data.get("hastranslationdata", 0),
        }
    )
    _insert(cursor, "CSYSObject", row)


def insert_csys_object_field(cursor, field: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "fieldid": field["fieldid"],
            "objectid": field["objectid"],
            "name": field["name"],
            "description": field.get("description"),
            "status": field.get("status", "Pub"),
            "state": field.get("state", "Active"),
            "ostate": field.get("ostate", "Active"),
            "nameunc": field["nameunc"],
            "isprimarykey": field.get("isprimarykey", 0),
            "datatype": field.get("datatype"),
            "systemdatatype": field.get("systemdatatype"),
            "displaytype": field.get("displaytype"),
            "fieldorder": field.get("fieldorder"),
            "maxlength": field.get("maxlength"),
            "precision": field.get("precision"),
            "scale": field.get("scale"),
            "isnullable": field.get("isnullable"),
            "isvirtual": field.get("isvirtual", 0),
            "hastranslationdata": field.get("hastranslationdata", 0),
        }
    )
    _insert(cursor, "CSYSObjectField", row)


def insert_csys_object_reference(cursor, reference: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "fieldid": reference["fieldid"],
            "objectid": reference["objectid"],
            "referencedobjectid": reference.get("referencedobjectid"),
            "name": reference["name"],
            "description": reference.get("description"),
            "status": reference.get("status", "Pub"),
            "state": reference.get("state", "Active"),
            "ostate": reference.get("ostate", "Active"),
            "nameunc": reference["nameunc"],
            "isprimarykey": reference.get("isprimarykey", 0),
            "referencedtype": reference.get("referencedtype", "Picklist"),
            "isnullable": reference.get("isnullable", 1),
            "relationtype": reference.get("relationtype", "Reference"),
            "constraintname": reference.get("constraintname"),
            "isvirtual": reference.get("isvirtual", 0),
            "hastranslationdata": reference.get("hastranslationdata", 0),
        }
    )
    _insert(cursor, "CSYSObjectReference", row)


def insert_csys_object_layout(cursor, data: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "objectlayoutid": data["objectlayoutid"],
            "objectid": data["objectid"],
            "name": data.get("name", "default layout"),
            "description": data.get("description"),
            "status": data.get("status", "Pub"),
            "state": data.get("state", "Active"),
            "ostate": data.get("ostate", "Active"),
            "isdefault": data.get("isdefault", 1),
            "isprefetch": data.get("isprefetch"),
            "type": data.get("type", "form"),
            "languagecode": data.get("languagecode"),
            "version": data.get("version"),
            "layoutkey": data.get("layoutkey"),
            "closebtnshow": data.get("closebtnshow"),
            "removebtnshow": data.get("removebtnshow"),
            "savebtnshow": data.get("savebtnshow"),
            "savenewbtnshow": data.get("savenewbtnshow"),
            "saveclosebtnshow": data.get("saveclosebtnshow"),
        }
    )
    _insert(cursor, "CSYSObjectLayout", row)


def insert_csys_object_layout_section(cursor, data: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "objectlayoutsectionid": data["objectlayoutsectionid"],
            "objectlayoutid": data["objectlayoutid"],
            "name": data["name"],
            "description": data.get("description"),
            "status": data.get("status", "Pub"),
            "state": data.get("state", "Active"),
            "ostate": data.get("ostate", "Active"),
            "isreferencecontainer": data.get("isreferencecontainer", 0),
            "issubsection": data.get("issubsection", 0),
            "type": data.get("type", "Section"),
            "data": data.get("data"),
            "workingdata": data.get("workingdata"),
            "objdataid": data.get("objdataid"),
            "keyname": data.get("keyname"),
            "isprefetch": data.get("isprefetch"),
            "oderposition": data.get("oderposition", 1),
            "visible": data.get("visible", 1),
        }
    )
    _insert(cursor, "CSYSObjectLayoutSection", row)


def insert_csys_object_layout_permission(cursor, data: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "objectlayoutpermissionid": data["objectlayoutpermissionid"],
            "name": data["name"],
            "description": data.get("description"),
            "status": data.get("status", "Pub"),
            "state": data.get("state", "Active"),
            "ostate": data.get("ostate", "Active"),
            "objectlayoutid": data["objectlayoutid"],
            "roleid": data["roleid"],
            "applicationid": data["applicationid"],
            "businessunitid": data.get("businessunitid"),
            "workflowid": data.get("workflowid"),
            "workflowstateid": data.get("workflowstateid"),
            "isnew": data.get("isnew", 1),
            "isreadonly": data.get("isreadonly"),
            "oderposition": data.get("oderposition", 10),
        }
    )
    _insert(cursor, "CSYSObjectLayoutPermission", row)


def insert_csys_view(cursor, data: Dict[str, Any]) -> None:
    compileddate = data.get("compileddate")
    if compileddate == "<NOW>" or compileddate is None:
        compileddate = datetime.now()

    row = _base_status_fields(
        {
            "viewid": data["viewid"],
            "name": data["name"],
            "description": data.get("description"),
            "status": data.get("status", "Pub"),
            "state": data.get("state", "Active"),
            "ostate": data.get("ostate", "Active"),
            "viewtype": data.get("viewtype", "default"),
            "refobjectid": data["refobjectid"],
            "command": data.get("command"),
            "serverdata": data.get("serverdata"),
            "clientdata": data.get("clientdata"),
            "compileddate": compileddate,
            "autorefresh": data.get("autorefresh", 1),
            "hidemenu": data.get("hidemenu"),
            "issystem": data.get("issystem"),
            "displaytype": data.get("displaytype", "grid"),
        }
    )
    _insert(cursor, "CSYSView", row)


def insert_csys_action(cursor, action: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "actionid": action["actionid"],
            "name": action["name"],
            "description": action.get("description"),
            "status": action.get("status", "Pub"),
            "state": action.get("state", "Active"),
            "ostate": action.get("ostate", "Active"),
            "type": action.get("type"),
            "objectaction": action.get("objectaction"),
            "baseobjectid": action.get("baseobjectid"),
            "objectid": action.get("objectid"),
            "showtype": action.get("showtype"),
            "command": action.get("command"),
            "paramoptions": action.get("paramoptions"),
            "workingdata": action.get("workingdata"),
            "serverdata": action.get("serverdata"),
            "clientdata": action.get("clientdata"),
            "compileddate": action.get("compileddate"),
        }
    )
    _insert(cursor, "CSYSAction", row)


def insert_csys_object_action(cursor, object_action: Dict[str, Any]) -> None:
    """
    Insere CSYSObjectAction.

    Correção importante:
    - O campo objectkeyid tem de ser preservado.
      Exemplos observados:
        Novo <Objeto>:
            objectid    = objeto base Listagem / CSYSView
            objectkeyid = viewid da listagem concreta

        <objeto>-listagem:
            objectid    = objeto base Layout / CSYSObjectLayout
            objectkeyid = objectlayoutid do layout concreto

    - Não forçamos objectid para NULL no executor.
      O planner é que deve decidir objectid e objectkeyid.
    """
    row = _base_status_fields(
        {
            "objectactionid": object_action["objectactionid"],
            "name": object_action["name"],
            "description": object_action.get("description"),

            "status": object_action.get("status", "Pub"),
            "state": object_action.get("state", "Active"),

            # No padrão funcional observado, CSYSObjectAction.ostate pode ficar NULL.
            # Por isso preservamos explicitamente None quando o planner envia None.
            "ostate": object_action.get("ostate"),

            "actionid": object_action["actionid"],
            "objectid": object_action.get("objectid"),
            "objectkeyid": object_action.get("objectkeyid"),

            "objectworkflowid": object_action.get("objectworkflowid"),
            "objectworkflowstateid": object_action.get("objectworkflowstateid"),

            "menuorder": object_action.get("menuorder"),
            "menupath": object_action.get("menupath"),
            "filter": object_action.get("filter"),
            "paramoptions": object_action.get("paramoptions"),
            "parameters": object_action.get("parameters"),

            "showtype": object_action.get("showtype", "toolbarbuttom"),
            "iconresourceunc": object_action.get("iconresourceunc"),
            "showisnew": object_action.get("showisnew"),
            "showhasreadaccess": object_action.get("showhasreadaccess"),
            "showhaswriteaccess": object_action.get("showhaswriteaccess", 1),
            "showalways": object_action.get("showalways"),
            "hideaction": object_action.get("hideaction"),
            "musthaverecord": object_action.get("musthaverecord"),
            "keyname": object_action.get("keyname"),
            "forcerefresh": object_action.get("forcerefresh"),
            "parentclose": object_action.get("parentclose"),
            "savefirst": object_action.get("savefirst"),
            "autoexecute": object_action.get("autoexecute"),
        }
    )
    _insert(cursor, "CSYSObjectAction", row)


def insert_csys_permission(cursor, permission: Dict[str, Any]) -> None:
    row = _base_status_fields(
        {
            "permissionid": permission["permissionid"],
            "name": permission["name"],
            "description": permission.get("description"),
            "status": permission.get("status", "Pub"),
            "state": permission.get("state", "Active"),
            "ostate": permission.get("ostate", "Active"),
            "type": permission.get("type"),
            "applicationid": permission.get("applicationid"),
            "baseobjectid": permission.get("baseobjectid"),
            "definition": permission.get("definition"),
        }
    )
    _insert(cursor, "CSYSPermission", row)


def insert_csys_role_permission(
    cursor,
    *,
    permission: Dict[str, Any],
    role_permission: Dict[str, Any],
) -> None:
    role_name = role_permission.get("role_name") or "Role"

    row = _base_status_fields(
        {
            "rolepermissionid": _new_guid(),
            "name": role_name,
            "description": role_permission.get("description"),
            "status": role_permission.get("status", "Pub"),
            "state": role_permission.get("state", "Active"),
            "ostate": role_permission.get("ostate", "Active"),
            "roleid": role_permission["roleid"],
            "permissionid": permission["permissionid"],
            "workflowid": role_permission.get("workflowid"),
            "workflowstateid": role_permission.get("workflowstateid"),
            "ownerpermissionmask": role_permission.get("ownerpermissionmask"),
            "bupermissionmask": role_permission.get("bupermissionmask"),
            "budescpermissionmask": role_permission.get("budescpermissionmask"),
            "fullpermissionmask": role_permission.get("fullpermissionmask"),
        }
    )
    _insert(cursor, "CSYSRolePermission", row)


# ---------------------------------------------------------------------
# Execução do plano
# ---------------------------------------------------------------------

def _extract_operation(object_plan: Dict[str, Any], operation_name: str) -> Optional[Dict[str, Any]]:
    for operation in object_plan.get("operations") or []:
        if operation.get("operation") == operation_name:
            return operation
    return None


def _execute_object_plan(
    cursor,
    object_plan: Dict[str, Any],
    placeholder_map: Dict[str, str],
    execution_log: List[Dict[str, Any]],
) -> None:
    object_name = object_plan.get("source_object")
    table_name = object_plan.get("table_name")

    def log(step: int, operation: str, status: str, detail: str = "") -> None:
        execution_log.append(
            {
                "object": object_name,
                "table": table_name,
                "step": step,
                "operation": operation,
                "status": status,
                "detail": detail,
            }
        )

    # Resolver placeholders de todo o object_plan de uma vez.
    resolved_plan = _resolve_placeholders(object_plan, placeholder_map)

    # 1. CSYSObject
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObject")
    if op:
        insert_csys_object(cursor, op["data"])
        log(op["step"], op["operation"], "inserted", op["data"]["nameunc"])

    # 2. CSYSObjectField
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectField_BATCH")
    if op:
        for field in op.get("data") or []:
            insert_csys_object_field(cursor, field)
        log(op["step"], op["operation"], "inserted", f"{len(op.get('data') or [])} fields")

    # 3. CSYSObjectReference
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectReference_BATCH")
    if op:
        for reference in op.get("data") or []:
            insert_csys_object_reference(cursor, reference)
        log(op["step"], op["operation"], "inserted", f"{len(op.get('data') or [])} references")

    # 4. CSYSView
    # Importante:
    # CSYSObjectLayoutSection.objdataid tem FK para CSYSView.viewid.
    # Por isso, a View tem de existir ANTES de inserir a LayoutSection.
    # O plano pode continuar a numerar a View como step 7, mas a execução
    # tem de respeitar as dependências reais da base de dados.
    op = _extract_operation(resolved_plan, "UPSERT_CSYSView")
    if op:
        insert_csys_view(cursor, op["data"])
        log(op["step"], op["operation"], "inserted", op["data"]["name"])

    # 5. CSYSObjectLayout
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectLayout")
    if op:
        insert_csys_object_layout(cursor, op["data"])
        log(op["step"], op["operation"], "inserted", op["data"]["name"])

    # 6. CSYSObjectLayoutSection
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectLayoutSection")
    if op:
        section_data = dict(op["data"])
        section_data.pop("layout_rule", None)
        insert_csys_object_layout_section(cursor, section_data)
        log(op["step"], op["operation"], "inserted", section_data["name"])

    # 7. CSYSObjectLayoutPermission
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectLayoutPermission")
    if op:
        insert_csys_object_layout_permission(cursor, op["data"])
        log(op["step"], op["operation"], "inserted", op["data"]["name"])

    # 8. CSYSAction
    op = _extract_operation(resolved_plan, "UPSERT_CSYSAction_BATCH")
    if op:
        for action in op.get("data") or []:
            insert_csys_action(cursor, action)
        log(op["step"], op["operation"], "inserted", f"{len(op.get('data') or [])} actions")

    # 9. CSYSObjectAction
    # O executor já não reescreve objectid.
    # A partir de agora, o planner tem de enviar:
    # - objectid correto
    # - objectkeyid correto
    # - flags show/save/refresh corretas
    op = _extract_operation(resolved_plan, "UPSERT_CSYSObjectAction_BATCH")
    if op:
        object_actions = op.get("data") or []

        for object_action in object_actions:
            insert_csys_object_action(cursor, object_action)

        log(op["step"], op["operation"], "inserted", f"{len(object_actions)} object actions")

    # 10. Permissions
    op = _extract_operation(resolved_plan, "UPSERT_PERMISSIONS_AND_ROLE_PERMISSIONS")
    if op:
        permission_groups = op.get("data") or []
        role_permission_count = 0

        for item in permission_groups:
            permission = item["permission"]
            insert_csys_permission(cursor, permission)

            for role_permission in item.get("role_permissions") or []:
                insert_csys_role_permission(
                    cursor,
                    permission=permission,
                    role_permission=role_permission,
                )
                role_permission_count += 1

        log(
            op["step"],
            op["operation"],
            "inserted",
            f"{len(permission_groups)} permissions, {role_permission_count} role permissions",
        )


def execute_framework_metadata(
    blueprint: Dict[str, Any],
    *,
    dry_run: bool = True,
    execute: bool = False,
    confirm_phrase: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executa ou simula a criação da metadata da framework.
    """
    preflight = run_framework_metadata_preflight(blueprint)
    plan = preflight.get("plan") or {}

    env_enabled = is_framework_execution_enabled()
    confirmation_ok = confirm_phrase == CONFIRM_PHRASE

    if dry_run or not execute:
        return {
            "success": True,
            "mode": "dry_run",
            "safe_mode": "NO_DATABASE_WRITES",
            "executed": False,
            "can_execute": preflight.get("can_execute", False),
            "blocking_issues": preflight.get("blocking_issues", []),
            "warnings": preflight.get("warnings", []),
            "adapter_warnings": preflight.get("adapter_warnings", []),
            "message": "Dry-run concluído. Nenhuma metadata foi escrita.",
            "plan": plan,
            "preflight": preflight,
        }

    if not env_enabled:
        return {
            "success": False,
            "mode": "blocked",
            "safe_mode": "ENV_BLOCKED",
            "executed": False,
            "can_execute": False,
            "message": f"Execução bloqueada: define {ENABLE_ENV}=true no .env.",
            "blocking_issues": [f"{ENABLE_ENV} não está ativo."],
            "preflight": preflight,
        }

    if not confirmation_ok:
        return {
            "success": False,
            "mode": "blocked",
            "safe_mode": "CONFIRMATION_REQUIRED",
            "executed": False,
            "can_execute": False,
            "message": f"Execução bloqueada: confirm_phrase deve ser {CONFIRM_PHRASE!r}.",
            "blocking_issues": ["Frase de confirmação inválida ou ausente."],
            "preflight": preflight,
        }

    if not preflight.get("can_execute"):
        return {
            "success": False,
            "mode": "preflight_blocked",
            "safe_mode": "PREFLIGHT_BLOCKED",
            "executed": False,
            "can_execute": False,
            "message": "Execução bloqueada pelo preflight.",
            "blocking_issues": preflight.get("blocking_issues", []),
            "warnings": preflight.get("warnings", []),
            "adapter_warnings": preflight.get("adapter_warnings", []),
            "preflight": preflight,
        }

    placeholder_map: Dict[str, str] = {}
    execution_log: List[Dict[str, Any]] = []

    conn = connect_pymssql()

    try:
        cursor = conn.cursor()

        for object_plan in plan.get("object_plans") or []:
            _execute_object_plan(cursor, object_plan, placeholder_map, execution_log)

        conn.commit()

        return {
            "success": True,
            "mode": "execute",
            "safe_mode": "EXECUTED_WITH_CONFIRMATION",
            "executed": True,
            "message": "Metadata da framework criada com sucesso.",
            "placeholder_map": placeholder_map,
            "execution_log": execution_log,
            "preflight": {
                "can_execute": preflight.get("can_execute"),
                "blocking_issues": preflight.get("blocking_issues", []),
                "warnings": preflight.get("warnings", []),
                "adapter_warnings": preflight.get("adapter_warnings", []),
            },
        }

    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass

        return {
            "success": False,
            "mode": "error_rolled_back",
            "safe_mode": "TRANSACTION_ROLLED_BACK",
            "executed": False,
            "message": f"Erro ao criar metadata da framework. Transação revertida: {exc}",
            "error": str(exc),
            "execution_log": execution_log,
            "placeholder_map": placeholder_map,
        }

    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def _load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("schema"), dict):
        return data["schema"]

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]

    if not isinstance(data, dict):
        raise ValueError("O ficheiro JSON tem de conter um objeto JSON.")

    return data


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print(
            "Uso:\n"
            "  python3 framework_metadata_executor.py blueprint.json\n"
            "  python3 framework_metadata_executor.py blueprint.json --execute EXECUTE_FRAMEWORK_METADATA"
        )
        return 2

    blueprint = _load_json_file(argv[1])

    execute = False
    confirm_phrase = None

    if len(argv) >= 4 and argv[2] == "--execute":
        execute = True
        confirm_phrase = argv[3]

    result = execute_framework_metadata(
        blueprint,
        dry_run=not execute,
        execute=execute,
        confirm_phrase=confirm_phrase,
    )

    print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2, default=str))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
