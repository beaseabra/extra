# ===== framework_object_planner.py =====
# AiBizCore — Framework Object Planner
#
# Objetivo:
# - Receber um blueprint AiBizCore.
# - Converter o blueprint para o modelo SQL Server já usado pelo projeto.
# - Gerar um PLANO DRY-RUN da metadata necessária para criar objetos lógicos
#   na framework AiBizCore/ecobite.
#
# Segurança:
# - Este ficheiro NÃO executa INSERT/UPDATE/DELETE.
# - Não altera a base de dados.
# - Só produz JSON com o plano técnico a executar no futuro.
#
# Uso direto:
#   python3 framework_object_planner.py blueprint.json > framework_plan.json
#
# Uso por import:
#   from framework_object_planner import plan_framework_metadata_from_blueprint
#   plan = plan_framework_metadata_from_blueprint(schema)

from __future__ import annotations

import json
import re
import sys
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sql_server_schema_adapter import convert_blueprint_to_sqlserver_schema


# ---------------------------------------------------------------------
# IDs fixos confirmados na framework Demo_AIBC_Demo4
# ---------------------------------------------------------------------

FRAMEWORK_REFERENCE_IDS = {
    "application_tab_auxiliares": "E5F41C8F-5DF0-4955-BCBC-4FD21DD10469",
    "role_designer": "0A1958E3-5D3A-44DF-B9D4-3EDBD680BB94",
    "role_adm_tab_auxiliares": "8A85CE1D-A80B-479A-B417-D66C9BD48467",
    "base_object_utilizador_base": "28495E60-D823-4BC6-AF46-2ED4A54E2A2E",
    "base_object_listagem": "7B4FF922-86F4-457B-8E4C-FF5DC82CDC18",
    "base_object_layout": "9C6BAAB6-E288-4463-8F4A-CB754A822A3C",
}

DEFAULT_VALUE_XML = """ <data>
  <status>Pub</status><state>Active</state><ostate>Active</ostate><locked>false</locked>
  <descendantlocked>false</descendantlocked>
  </data>"""

SYSTEM_OBJECT_FIELD_DISPLAY_NAMES = {
    "creationdate": "Criado em",
    "description": "Descrição",
    "modificationdate": "Modificado em",
    "name": "Nome",
    "descendantlocked": "descendantlocked",
    "locked": "locked",
    "ostate": "ostate",
    "state": "state",
    "status": "status",
}

SYSTEM_REFERENCE_DISPLAY_NAMES = {
    "creationuserid": "Criado por",
    "modificationuserid": "Modificado por",
}

EDITABLE_LAYOUT_FIELDS = ["descendantlocked", "description", "name"]

ADMINISTRATION_FIELD_ORDER = [
    "creationdate",
    "locked",
    "modificationdate",
    "ostate",
    "state",
    "status",
    "creationuserid",
    "modificationuserid",
]


# ---------------------------------------------------------------------
# Helpers de normalização
# ---------------------------------------------------------------------

def _strip_accents(value: str) -> str:
    value = str(value or "")
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _split_tokens(value: str) -> List[str]:
    value = _strip_accents(str(value or ""))
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value)
    value = value.strip("_").lower()
    return [token for token in value.split("_") if token]


def _title_from_tokens(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    return " ".join(token[:1].upper() + token[1:] for token in tokens)


def _human_field_name_from_source(
    *,
    source_field: str,
    source_object: str,
    technical_column: str,
) -> str:
    """
    Gera o nome visível de campo para a framework.

    Exemplo observado:
        codigo_teste -> Codigo
        valor_teste  -> Valor
        data_teste   -> Data

    Regra:
    - Usa source_field quando existe.
    - Remove sufixos evidentes ligados ao objeto quando só poluem o nome do campo.
    - Remove acentos para ficar consistente com o valor observado em BD: "Codigo".
    """
    raw = source_field or technical_column
    tokens = _split_tokens(raw)
    object_tokens = _split_tokens(source_object)

    if len(tokens) > 1 and object_tokens:
        # Caso comum em campos de teste: codigo_teste, valor_teste, data_teste.
        if tokens[-1] == object_tokens[0]:
            tokens = tokens[:-1]

        # Caso em que o campo termina com o nome completo do objeto em tokens.
        if len(tokens) > len(object_tokens) and tokens[-len(object_tokens):] == object_tokens:
            tokens = tokens[:-len(object_tokens)]

    if not tokens:
        tokens = _split_tokens(technical_column)

    return _title_from_tokens(tokens) or technical_column


def _parse_nvarchar_length(datatype: str, default: int = 255) -> int:
    value = str(datatype or "").lower()
    match = re.search(r"nvarchar\s*\((max|\d+)\)", value)
    if not match:
        return default

    group = match.group(1)
    if group == "max":
        return 2048

    try:
        return int(group)
    except ValueError:
        return default


def _framework_type_from_sql_field(field: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converte o tipo SQL Server do adapter para o formato observado em CSYSObjectField.
    """
    sql_type = str(field.get("datatype") or "").strip().lower()
    is_pk = bool(field.get("primary_key"))

    if "uniqueidentifier" in sql_type:
        return {
            "datatype": "Guid",
            "systemdatatype": 36,
            "displaytype": "Guid",
            "maxlength": 16,
            "precision": 0,
            "scale": 0,
            "isnullable": 0 if is_pk else 1,
        }

    if sql_type in {"date", "datetime", "datetime2", "datetimeoffset"} or sql_type.startswith("date"):
        return {
            "datatype": "DateTimeOffset",
            "systemdatatype": 43,
            "displaytype": "DateTime",
            "maxlength": 10,
            "precision": 34,
            "scale": 7,
            "isnullable": 1,
        }

    if sql_type == "bit":
        return {
            "datatype": "Boolean",
            "systemdatatype": 104,
            "displaytype": "Boolean",
            "maxlength": 1,
            "precision": 1,
            "scale": 0,
            "isnullable": 1,
        }

    if sql_type.startswith("decimal") or sql_type in {"numeric", "money", "float", "real"}:
        return {
            "datatype": "Number",
            "systemdatatype": 106,
            "displaytype": "Number",
            "maxlength": 9,
            "precision": 18,
            "scale": 2,
            "isnullable": 1,
        }

    if sql_type in {"int", "bigint", "smallint", "tinyint"}:
        return {
            "datatype": "Number",
            "systemdatatype": 56,
            "displaytype": "Number",
            "maxlength": 9,
            "precision": 10,
            "scale": 0,
            "isnullable": 1,
        }

    return {
        "datatype": "String",
        "systemdatatype": 231,
        "displaytype": "String",
        "maxlength": _parse_nvarchar_length(sql_type, default=255),
        "precision": 0,
        "scale": 0,
        "isnullable": 1,
    }


def _placeholder(kind: str, name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "").strip()).strip("_")
    return f"<{kind}:{cleaned or 'value'}>"


def _ordered_unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


# ---------------------------------------------------------------------
# Construção de metadata planeada
# ---------------------------------------------------------------------

def _find_relation_for_field(
    *,
    relations: Sequence[Dict[str, Any]],
    from_table: str,
    from_field: str,
) -> Optional[Dict[str, Any]]:
    for relation in relations:
        if (
            str(relation.get("from_table") or "").lower() == from_table.lower()
            and str(relation.get("from_field") or "").lower() == from_field.lower()
        ):
            return relation
    return None


def _target_table_pk(tables_by_name: Dict[str, Dict[str, Any]], table_name: str) -> str:
    table = tables_by_name.get(table_name.lower())
    if table:
        return str(table.get("primary_key") or "id")
    if table_name.lower() == "csysuser":
        return "userid"
    return "id"


def _target_object_label(tables_by_name: Dict[str, Dict[str, Any]], table_name: str) -> str:
    table = tables_by_name.get(table_name.lower())
    if table:
        return str(table.get("source_object") or table.get("table_name") or table_name)
    if table_name.lower() == "csysuser":
        return "Utilizador (base)"
    return table_name


def build_object_fields_and_references(
    *,
    table: Dict[str, Any],
    tables_by_name: Dict[str, Dict[str, Any]],
    relations: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Divide campos SQL em:
    - CSYSObjectField planeados
    - CSYSObjectReference planeadas

    Observação do processo manual:
    - creationuserid e modificationuserid aparecem em CSYSObjectReference, não em CSYSObjectField.
    - Campos de relação também devem ser referências.
    """
    object_id = _placeholder("OBJECT_ID", table["table_name"])
    fields: List[Dict[str, Any]] = []
    references: List[Dict[str, Any]] = []

    for field in table.get("fields") or []:
        column = str(field.get("campo") or "").strip()
        if not column:
            continue

        relation = _find_relation_for_field(
            relations=relations,
            from_table=table["table_name"],
            from_field=column,
        )

        is_reference = column in SYSTEM_REFERENCE_DISPLAY_NAMES or bool(relation)

        if is_reference:
            to_table = str((relation or {}).get("to_table") or "CSYSUser")
            target_pk = _target_table_pk(tables_by_name, to_table)

            if to_table.lower() == "csysuser":
                referenced_object_id = FRAMEWORK_REFERENCE_IDS["base_object_utilizador_base"]
            else:
                referenced_object_id = _placeholder("OBJECT_ID", to_table)

            references.append(
                {
                    "operation": "UPSERT_CSYSObjectReference",
                    "fieldid": _placeholder("FIELD_ID", f"{table['table_name']}_{column}"),
                    "objectid": object_id,
                    "referencedobjectid": referenced_object_id,
                    "name": SYSTEM_REFERENCE_DISPLAY_NAMES.get(
                        column,
                        _target_object_label(tables_by_name, to_table),
                    ),
                    "nameunc": column,
                    "isprimarykey": 0,
                    "referencedtype": "Picklist",
                    "isnullable": 1,
                    "relationtype": "Reference",
                    "constraintname": f"FK_{table['table_name']}_{column}_{to_table}_{target_pk}",
                    "status": "Pub",
                    "state": "Active",
                    "ostate": "Active",
                    "source_relation": relation,
                }
            )
            continue

        type_info = _framework_type_from_sql_field(field)

        if bool(field.get("primary_key")):
            visible_name = column
        elif column in SYSTEM_OBJECT_FIELD_DISPLAY_NAMES:
            visible_name = SYSTEM_OBJECT_FIELD_DISPLAY_NAMES[column]
        else:
            visible_name = _human_field_name_from_source(
                source_field=str(field.get("source_field") or column),
                source_object=str(table.get("source_object") or ""),
                technical_column=column,
            )

        if column == "description":
            type_info["maxlength"] = 2048

        fields.append(
            {
                "operation": "UPSERT_CSYSObjectField",
                "fieldid": _placeholder("FIELD_ID", f"{table['table_name']}_{column}"),
                "objectid": object_id,
                "name": visible_name,
                "nameunc": column,
                "isprimarykey": 1 if bool(field.get("primary_key")) else 0,
                "datatype": type_info["datatype"],
                "systemdatatype": type_info["systemdatatype"],
                "displaytype": type_info["displaytype"],
                "fieldorder": None,
                "maxlength": type_info["maxlength"],
                "precision": type_info["precision"],
                "scale": type_info["scale"],
                "isnullable": type_info["isnullable"],
                "status": "Pub",
                "state": "Active",
                "ostate": "Active",
                "source": field.get("source"),
                "source_field": field.get("source_field"),
                "source_type": field.get("source_type"),
            }
        )

    return fields, references


def _field_block(field_name: str, readonly: bool, required: bool = False) -> str:
    label_class = " field required" if required else " field "
    container = "fieldcontainerreadonly" if readonly else "fieldcontainer"
    return (
        f'<div class="dfieldblock"><div class="label"><label for="id_{field_name}" '
        f'class="{label_class}" data-bind="style:{{color:datachange.{field_name}_label}}">'
        f'<span class="labelcontainer">{field_name}</span></label></div>'
        f'<div class="field" ><span class="{container}">{field_name}</span></div></div>'
    )


def build_layout_workingdata(
    *,
    object_display_name: str,
    pk_name: str,
    fields: Sequence[Dict[str, Any]],
    references: Sequence[Dict[str, Any]],
) -> str:
    field_names = [str(field.get("nameunc")) for field in fields if field.get("nameunc")]
    reference_names = [str(ref.get("nameunc")) for ref in references if ref.get("nameunc")]

    user_field_names = [
        str(field.get("nameunc"))
        for field in fields
        if field.get("source") == "user_field"
    ]

    first_tab = _ordered_unique([
        "descendantlocked",
        "description",
        "name",
        *user_field_names,
    ])

    admin_tab = _ordered_unique([
        pk_name,
        *ADMINISTRATION_FIELD_ORDER,
    ])

    available = set(field_names) | set(reference_names)
    first_tab = [name for name in first_tab if name in available]
    admin_tab = [name for name in admin_tab if name in available]

    first_html = "\n".join(
        _field_block(name, readonly=(name not in EDITABLE_LAYOUT_FIELDS), required=False)
        for name in first_tab
    )

    admin_html = "\n".join(
        _field_block(name, readonly=True, required=(name == pk_name))
        for name in admin_tab
    )

    return f'''<div id="frontdata">
  <div id="tabstrip" data-role="tabstrip" data-animation='{{"open":{{"effects":"fadeIn"}}}}' >
    <ul>
      <li>{object_display_name}</li>
      <li>Administração</li>
    </ul>
    <div class="content">
{first_html}
    </div>
    <div class="content">
{admin_html}
    </div>
  </div>
</div>'''


def build_view_serverdata(*, table_name: str, pk_name: str) -> str:
    return f'''<data>
  <viewtype>select</viewtype>
  <columns jsondisplaytype="list">
    <column><field>t01.{pk_name}</field><alias>{pk_name}</alias></column>
    <column><field>t01.description</field><alias>description</alias></column>
    <column><field>t01.name</field><alias>name</alias></column>
  </columns>
  <from haswhere="1">
    {table_name} t01 with(nolock)
    where t01.state='Active'
  </from>
  <orderby jsondisplaytype="list">
    <field>t01.name asc</field>
  </orderby>
</data>'''


def build_view_clientdata(*, object_display_name: str, pk_name: str) -> str:
    return f'''<data>
  <definition>
    <name>{object_display_name}</name>
    <description>{object_display_name}</description>
    <datasource>
      <pageSize datatype="number">100</pageSize>
      <serverPaging datatype="boolean">true</serverPaging>
      <serverFiltering datatype="boolean">true</serverFiltering>
      <serverSorting datatype="boolean">true</serverSorting>
    </datasource>
    <grid>
      <rowtemplate/>
      <altrowtemplate/>
      <selectable>row</selectable>
      <navigatable datatype="boolean">true</navigatable>
      <height>100%</height>
      <resizable datatype="boolean">true</resizable>
      <filterable datatype="boolean">true</filterable>
      <sortable datatype="boolean">true</sortable>
      <pageable>
        <refresh datatype="boolean">true</refresh>
        <pageSizes datatype="boolean">true</pageSizes>
        <buttonCount datatype="number">5</buttonCount>
      </pageable>
      *       <columnMenu datatype="boolean">true</columnMenu>
      *       <columnMenu><componentType>tabbed</componentType></columnMenu>
      <reorderable datatype="boolean">true</reorderable>
      <editable datatype="boolean">false</editable>
      <toolbar/>
    </grid>
  </definition>
  <keyfield>{pk_name}</keyfield>
  <modelschema>
    <{pk_name}><type>string</type></{pk_name}>
    <name><type>string</type></name>
    <description><type>string</type></description>
  </modelschema>
  <columns jsondisplaytype="list">
    <col>
      <field>name</field>
      <title>Nome</title>
      <width datatype="number">100</width>
      <filterable datatype="boolean">true</filterable>
      <template>&lt;a href="javascript:" onclick="flink(this,'${{{pk_name}}}',null,null)" &gt;${{name}}&lt;/a&gt;</template>
    </col>
    <col>
      <field>description</field>
      <title>Descrição</title>
      <width datatype="number">100</width>
      <filterable datatype="boolean">true</filterable>
    </col>
  </columns>
</data>'''


def build_object_metadata_plan(
    *,
    table: Dict[str, Any],
    tables_by_name: Dict[str, Dict[str, Any]],
    relations: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    object_name = str(table.get("source_object") or table.get("english_entity") or table["table_name"])
    table_name = str(table["table_name"])
    pk_name = str(table["primary_key"])

    object_id = _placeholder("OBJECT_ID", table_name)
    layout_id = _placeholder("LAYOUT_ID", table_name)
    layout_section_id = _placeholder("LAYOUT_SECTION_ID", table_name)
    view_id = _placeholder("VIEW_ID", table_name)
    action_new_id = _placeholder("ACTION_ID", f"{table_name}_Novo")
    action_view_id = _placeholder("ACTION_ID", f"{table_name}_Listagem")

    fields, references = build_object_fields_and_references(
        table=table,
        tables_by_name=tables_by_name,
        relations=relations,
    )

    layout_workingdata = build_layout_workingdata(
        object_display_name=object_name,
        pk_name=pk_name,
        fields=fields,
        references=references,
    )

    serverdata = build_view_serverdata(table_name=table_name, pk_name=pk_name)
    clientdata = build_view_clientdata(object_display_name=object_name, pk_name=pk_name)

    return {
        "source_object": object_name,
        "table_name": table_name,
        "primary_key": pk_name,
        "planned_ids": {
            "objectid": object_id,
            "objectlayoutid": layout_id,
            "objectlayoutsectionid": layout_section_id,
            "viewid": view_id,
            "action_new_id": action_new_id,
            "action_view_id": action_view_id,
        },
        "operations": [
            {
                "step": 1,
                "operation": "UPSERT_CSYSObject",
                "table": "CSYSObject",
                "data": {
                    "objectid": object_id,
                    "name": object_name,
                    "description": None,
                    "nameunc": table_name,
                    "pkfieldname": pk_name,
                    "status": "Pub",
                    "state": "Active",
                    "ostate": "Active",
                    "locked": 0,
                    "descendantlocked": 0,
                    "issystem": 0,
                    "ishiden": 0,
                    "ispublished": 0,
                    "defaultvalue": DEFAULT_VALUE_XML,
                    "baseapplicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                    "hidemenu": 0,
                },
            },
            {
                "step": 2,
                "operation": "UPSERT_CSYSObjectField_BATCH",
                "table": "CSYSObjectField",
                "count": len(fields),
                "data": fields,
            },
            {
                "step": 3,
                "operation": "UPSERT_CSYSObjectReference_BATCH",
                "table": "CSYSObjectReference",
                "count": len(references),
                "data": references,
            },
            {
                "step": 4,
                "operation": "UPSERT_CSYSObjectLayout",
                "table": "CSYSObjectLayout",
                "data": {
                    "objectlayoutid": layout_id,
                    "objectid": object_id,
                    "name": "default layout",
                    "description": None,
                    "type": "form",
                    "isdefault": 1,
                    "issystem": None,
                    "closebtnshow": 1,
                    "removebtnshow": 1,
                    "savebtnshow": 1,
                    "savenewbtnshow": 0,
                    "saveclosebtnshow": 1,
                    "status": "Pub",
                    "state": "Active",
                    "ostate": "Active",
                },
            },
            {
                "step": 5,
                "operation": "UPSERT_CSYSObjectLayoutSection",
                "table": "CSYSObjectLayoutSection",
                "data": {
                    "objectlayoutsectionid": layout_section_id,
                    "objectlayoutid": layout_id,
                    "name": object_name,
                    "description": None,
                    "type": "Section",
                    "visible": 1,
                    "oderposition": 1,
                    "isreferencecontainer": 0,
                    "issubsection": 0,
                    "workingdata": layout_workingdata,
                    "objdataid": view_id,
                    "layout_rule": {
                        "editable_fields": EDITABLE_LAYOUT_FIELDS,
                        "readonly_fields": [
                            name
                            for name in _ordered_unique(
                                [
                                    *(field.get("nameunc") for field in fields),
                                    *(reference.get("nameunc") for reference in references),
                                ]
                            )
                            if name not in EDITABLE_LAYOUT_FIELDS
                        ],
                    },
                },
            },
            {
                "step": 6,
                "operation": "UPSERT_CSYSObjectLayoutPermission",
                "table": "CSYSObjectLayoutPermission",
                "data": {
                    "objectlayoutpermissionid": _placeholder("LAYOUT_PERMISSION_ID", table_name),
                    "name": "Adm Tab. Auxiliares",
                    "objectlayoutid": layout_id,
                    "roleid": FRAMEWORK_REFERENCE_IDS["role_adm_tab_auxiliares"],
                    "applicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                    "isnew": 1,
                    "isreadonly": None,
                    "oderposition": 10,
                    "status": "Pub",
                    "state": "Active",
                    "ostate": "Active",
                },
            },
            {
                "step": 7,
                "operation": "UPSERT_CSYSView",
                "table": "CSYSView",
                "data": {
                    "viewid": view_id,
                    "name": object_name,
                    "description": None,
                    "viewtype": "default",
                    "refobjectid": object_id,
                    "command": json.dumps(
                        {
                            "width": 300,
                            "height": 400,
                            "title": f"Open View - {object_name}",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "serverdata": serverdata,
                    "clientdata": clientdata,
                    "compileddate": "<NOW>",
                    "autorefresh": 1,
                    "displaytype": "grid",
                    "issystem": 1,
                    "status": "Pub",
                    "state": "Active",
                    "ostate": "Active",
                },
            },
            {
                "step": 8,
                "operation": "UPSERT_CSYSAction_BATCH",
                "table": "CSYSAction",
                "count": 2,
                "data": [
                    {
                        "actionid": action_new_id,
                        "name": f"{object_name} - Novo",
                        "description": None,
                        "type": "object",
                        "objectaction": "new",
                        "baseobjectid": object_id,
                        "objectid": None,
                        "status": "Pub",
                        "state": "Active",
                        "ostate": "Active",
                    },
                    {
                        "actionid": action_view_id,
                        "name": f"{object_name} - Listagem",
                        "description": None,
                        "type": "view",
                        "objectaction": None,
                        "baseobjectid": FRAMEWORK_REFERENCE_IDS["base_object_listagem"],
                        "objectid": view_id,
                        "status": "Pub",
                        "state": "Active",
                        "ostate": "Active",
                    },
                ],
            },
            {
                "step": 9,
                "operation": "UPSERT_CSYSObjectAction_BATCH",
                "table": "CSYSObjectAction",
                "count": 2,
                "data": [
                    {
                        "objectactionid": _placeholder("OBJECT_ACTION_ID", f"{table_name}_listagem"),
                        "name": f"{object_name.lower()}-listagem",
                        "actionid": action_view_id,
                        "objectid": FRAMEWORK_REFERENCE_IDS["base_object_layout"],
                        "objectkeyid": layout_id,
                        "showtype": "toolbarbuttom",
                        "showisnew": None,
                        "showhasreadaccess": None,
                        "showhaswriteaccess": 1,
                        "showalways": None,
                        "hideaction": None,
                        "musthaverecord": None,
                        "forcerefresh": 0,
                        "parentclose": None,
                        "savefirst": 1,
                        "autoexecute": None,
                        "keyname": None,
                        "status": "Pub",
                        "state": "Active",
                        "ostate": None,
                    },
                    {
                        "objectactionid": _placeholder("OBJECT_ACTION_ID", f"{table_name}_novo"),
                        "name": f"Novo {object_name}",
                        "actionid": action_new_id,
                        "objectid": FRAMEWORK_REFERENCE_IDS["base_object_listagem"],
                        "objectkeyid": view_id,
                        "showtype": "toolbarbuttom",
                        "showisnew": 0,
                        "showhasreadaccess": 0,
                        "showhaswriteaccess": 1,
                        "showalways": None,
                        "hideaction": None,
                        "musthaverecord": None,
                        "forcerefresh": 0,
                        "parentclose": None,
                        "savefirst": None,
                        "autoexecute": None,
                        "keyname": None,
                        "status": "Pub",
                        "state": "Active",
                        "ostate": None,
                    },
                ],
                "cleanup_rule": (
                    "Ações geradas de acordo com o padrão observado no objeto funcional "
                    "'Tipo Artigo': ação de listagem ligada ao objeto base Layout e ao layout "
                    "concreto; ação Novo ligada ao objeto base Listagem e à view concreta."
                ),
            },
            {
                "step": 10,
                "operation": "UPSERT_PERMISSIONS_AND_ROLE_PERMISSIONS",
                "tables": ["CSYSPermission", "CSYSRolePermission"],
                "data": [
                    {
                        "target": "object",
                        "permission": {
                            "permissionid": _placeholder("PERMISSION_ID", f"{table_name}_object"),
                            "name": "Tab. Auxiliares",
                            "type": "Object",
                            "applicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                            "baseobjectid": object_id,
                        },
                        "role_permissions": [
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_adm_tab_auxiliares"],
                                "role_name": "Adm Tab. Auxiliares",
                                "fullpermissionmask": 63,
                            },
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_designer"],
                                "role_name": "Designer",
                                "fullpermissionmask": 8192,
                            },
                        ],
                    },
                    {
                        "target": "view",
                        "permission": {
                            "permissionid": _placeholder("PERMISSION_ID", f"{table_name}_view"),
                            "name": "Tab. Auxiliares",
                            "type": "View",
                            "applicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                            "baseobjectid": view_id,
                        },
                        "role_permissions": [
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_adm_tab_auxiliares"],
                                "role_name": "Adm Tab. Auxiliares",
                                "fullpermissionmask": 1,
                            },
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_designer"],
                                "role_name": "Designer",
                                "fullpermissionmask": 8192,
                            },
                        ],
                    },
                    {
                        "target": "action_new",
                        "permission": {
                            "permissionid": _placeholder("PERMISSION_ID", f"{table_name}_action_new"),
                            "name": "Tab. Auxiliares",
                            "type": "Action",
                            "applicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                            "baseobjectid": action_new_id,
                        },
                        "role_permissions": [
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_adm_tab_auxiliares"],
                                "role_name": "Adm Tab. Auxiliares",
                                "fullpermissionmask": 1,
                            },
                        ],
                    },
                    {
                        "target": "action_view",
                        "permission": {
                            "permissionid": _placeholder("PERMISSION_ID", f"{table_name}_action_view"),
                            "name": "Tab. Auxiliares",
                            "type": "Action",
                            "applicationid": FRAMEWORK_REFERENCE_IDS["application_tab_auxiliares"],
                            "baseobjectid": action_view_id,
                        },
                        "role_permissions": [
                            {
                                "roleid": FRAMEWORK_REFERENCE_IDS["role_adm_tab_auxiliares"],
                                "role_name": "Adm Tab. Auxiliares",
                                "fullpermissionmask": 1,
                            },
                        ],
                    },
                ],
            },
        ],
    }


def plan_framework_metadata_from_blueprint(blueprint: Dict[str, Any]) -> Dict[str, Any]:
    """
    Gera o plano completo de metadata da framework a partir do blueprint.
    Não executa nada.
    """
    blueprint = blueprint if isinstance(blueprint, dict) else {}

    converted = convert_blueprint_to_sqlserver_schema(blueprint)
    tables = converted.get("tables") or []
    relations = converted.get("relations") or []
    tables_by_name = {
        str(table.get("table_name") or "").lower(): table
        for table in tables
        if table.get("table_name")
    }

    object_plans = [
        build_object_metadata_plan(
            table=table,
            tables_by_name=tables_by_name,
            relations=relations,
        )
        for table in tables
    ]

    return {
        "success": True,
        "mode": "framework_metadata_plan_dry_run",
        "safe_mode": "NO_DATABASE_WRITES",
        "warning": (
            "Este resultado é apenas um plano. Não executa INSERT, UPDATE, DELETE "
            "nem altera a framework."
        ),
        "framework_reference_ids": FRAMEWORK_REFERENCE_IDS,
        "input_summary": {
            "objects": len(blueprint.get("objects") or []),
            "relations": len(blueprint.get("relations") or []),
        },
        "converted_sqlserver_summary": {
            "tables": len(tables),
            "relations": len(relations),
            "warnings": converted.get("warnings") or [],
        },
        "object_plans": object_plans,
        "execution_order": [
            "1. Criar/validar tabelas físicas no SQL Server com o Database Agent.",
            "2. Criar CSYSObject.",
            "3. Criar CSYSObjectField.",
            "4. Criar CSYSObjectReference.",
            "5. Criar CSYSObjectLayout.",
            "6. Criar CSYSView com serverdata/clientdata.",
            "7. Criar/atualizar CSYSObjectLayoutSection.workingdata.",
            "8. Criar CSYSObjectLayoutPermission.",
            "9. Criar CSYSAction.",
            "10. Criar CSYSObjectAction com objectkeyid correto.",
            "11. Criar CSYSPermission.",
            "12. Criar CSYSRolePermission.",
            "13. Validar com framework_metadata_introspector.py.",
        ],
    }


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
        print("Uso: python3 framework_object_planner.py blueprint.json")
        return 2

    blueprint = _load_json_file(argv[1])
    plan = plan_framework_metadata_from_blueprint(blueprint)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
