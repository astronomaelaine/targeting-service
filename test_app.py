"""
Testes unitários para o targeting-rules-service (app.py).

Assim como no flag-service, este app.py cria um pool de conexão real
com o PostgreSQL e faz chamadas HTTP reais ao auth-service assim que o
módulo é importado. Este arquivo:

  1. Define variáveis de ambiente obrigatórias ANTES do import.
  2. Faz "patch" de `psycopg2.pool.SimpleConnectionPool` para não tentar
     conectar em um banco real.
  3. Importa/recarrega o módulo `app` já com esse mock no lugar.
  4. Faz "patch" de `app.requests.get` em cada teste para simular o
     auth-service (chave válida, inválida, timeout, indisponível etc).

Observação sobre `psycopg2.extras.Json`:
  O código usa `Json(rules_obj)` para serializar o campo `rules` antes
  de passar para `cur.execute(...)`. Como a classe `Json` não implementa
  `__eq__`, não dá para comparar diretamente com `==` (nem com
  `assert_called_with`). Por isso, os testes que verificam o SQL
  inspecionam `mock_cursor.execute.call_args` e checam o atributo
  `.adapted` do objeto `Json` capturado, que guarda o dict original.

Requisitos para rodar:
    pip install pytest pytest-mock

Executar com:
    pytest test_app_targeting.py -v
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

# --- Variáveis de ambiente exigidas pelo app.py na importação ---
os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost:5432/testdb")
os.environ.setdefault("AUTH_SERVICE_URL", "http://auth-service-test:8001")


@pytest.fixture
def app_module():
    """
    Importa (ou recarrega) o módulo `app` com o SimpleConnectionPool
    mockado, para evitar conexão real com o PostgreSQL.
    """
    with patch("psycopg2.pool.SimpleConnectionPool") as mock_pool_cls:
        mock_pool_cls.return_value = MagicMock()

        if "app" in sys.modules:
            module = importlib.reload(sys.modules["app"])
        else:
            import app as module

        yield module


@pytest.fixture
def client(app_module):
    """Cliente de teste do Flask."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def mock_db_conn(app_module):
    """
    Mocka o par (conexão, cursor) retornado por pool.getconn(), permitindo
    configurar o retorno de fetchone/fetchall/rowcount em cada teste.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    app_module.pool.getconn.return_value = mock_conn
    return mock_conn, mock_cursor


def mock_auth_ok(app_module):
    """Ajuda a simular uma resposta 200 do auth-service."""
    response = MagicMock()
    response.status_code = 200
    return patch.object(app_module.requests, "get", return_value=response)


def mock_auth_invalid(app_module):
    """Ajuda a simular uma resposta 401 do auth-service (chave inválida)."""
    response = MagicMock()
    response.status_code = 401
    return patch.object(app_module.requests, "get", return_value=response)


def get_execute_params(mock_cursor):
    """Extrai (query, params) da última chamada a cur.execute(...)."""
    args, _ = mock_cursor.execute.call_args
    query = args[0]
    params = args[1] if len(args) > 1 else ()
    return query, params


# ----------------------------------------------------------------------
# /health
# ----------------------------------------------------------------------


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


# ----------------------------------------------------------------------
# Middleware de autenticação (require_auth)
# ----------------------------------------------------------------------


def test_rules_sem_header_authorization(client):
    response = client.get("/rules/flag-a")
    assert response.status_code == 401
    assert "Authorization" in response.get_json()["error"]


def test_rules_com_chave_invalida(client, app_module):
    with mock_auth_invalid(app_module):
        response = client.get(
            "/rules/flag-a", headers={"Authorization": "Bearer chave-invalida"}
        )
    assert response.status_code == 401
    assert response.get_json()["error"] == "Chave de API inválida"


def test_rules_auth_service_timeout(client, app_module):
    with patch.object(
        app_module.requests, "get", side_effect=requests_lib.exceptions.Timeout
    ):
        response = client.get(
            "/rules/flag-a", headers={"Authorization": "Bearer qualquer"}
        )
    assert response.status_code == 504


def test_rules_auth_service_indisponivel(client, app_module):
    with patch.object(
        app_module.requests,
        "get",
        side_effect=requests_lib.exceptions.ConnectionError,
    ):
        response = client.get(
            "/rules/flag-a", headers={"Authorization": "Bearer qualquer"}
        )
    assert response.status_code == 503


# ----------------------------------------------------------------------
# POST /rules (create_rule)
# ----------------------------------------------------------------------


def test_create_rule_sucesso(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    rules_obj = {"country": ["BR"], "percentage": 50}
    mock_cursor.fetchone.return_value = {
        "flag_name": "nova-flag",
        "is_enabled": True,
        "rules": rules_obj,
    }

    with mock_auth_ok(app_module):
        response = client.post(
            "/rules",
            json={"flag_name": "nova-flag", "rules": rules_obj, "is_enabled": True},
            headers={"Authorization": "Bearer chave-valida"},
        )

    assert response.status_code == 201
    assert response.get_json()["flag_name"] == "nova-flag"

    # Confirma que o objeto foi serializado com Json(...) antes do INSERT
    _, params = get_execute_params(mock_cursor)
    assert params[0] == "nova-flag"
    assert params[1] is True
    assert isinstance(params[2], app_module.Json)
    assert params[2].adapted == rules_obj


def test_create_rule_sem_flag_name(client, app_module):
    with mock_auth_ok(app_module):
        response = client.post(
            "/rules",
            json={"rules": {"country": ["BR"]}},
            headers={"Authorization": "Bearer chave-valida"},
        )
    assert response.status_code == 400
    assert "flag_name" in response.get_json()["error"]


def test_create_rule_sem_rules(client, app_module):
    with mock_auth_ok(app_module):
        response = client.post(
            "/rules",
            json={"flag_name": "flag-sem-regra"},
            headers={"Authorization": "Bearer chave-valida"},
        )
    assert response.status_code == 400
    assert "rules" in response.get_json()["error"]


def test_create_rule_duplicada(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.execute.side_effect = app_module.psycopg2.IntegrityError()

    with mock_auth_ok(app_module):
        response = client.post(
            "/rules",
            json={"flag_name": "flag-existente", "rules": {"country": ["BR"]}},
            headers={"Authorization": "Bearer chave-valida"},
        )

    assert response.status_code == 409
    assert "já existe" in response.get_json()["error"]


# ----------------------------------------------------------------------
# GET /rules/<flag_name> (get_rule)
# ----------------------------------------------------------------------


def test_get_rule_encontrada(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.fetchone.return_value = {
        "flag_name": "flag-a",
        "is_enabled": True,
        "rules": {"country": ["BR"]},
    }

    with mock_auth_ok(app_module):
        response = client.get(
            "/rules/flag-a", headers={"Authorization": "Bearer chave-valida"}
        )

    assert response.status_code == 200
    assert response.get_json()["flag_name"] == "flag-a"


def test_get_rule_nao_encontrada(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.fetchone.return_value = None

    with mock_auth_ok(app_module):
        response = client.get(
            "/rules/nao-existe", headers={"Authorization": "Bearer chave-valida"}
        )

    assert response.status_code == 404


# ----------------------------------------------------------------------
# PUT /rules/<flag_name> (update_rule)
# ----------------------------------------------------------------------


def test_update_rule_sem_corpo(client, app_module):
    with mock_auth_ok(app_module):
        response = client.put(
            "/rules/flag-a",
            data="",
            content_type="application/json",
            headers={"Authorization": "Bearer chave-valida"},
        )
    assert response.status_code == 400


def test_update_rule_sem_campos_validos(client, app_module):
    with mock_auth_ok(app_module):
        response = client.put(
            "/rules/flag-a",
            json={"campo_invalido": "x"},
            headers={"Authorization": "Bearer chave-valida"},
        )
    assert response.status_code == 400
    assert "obrigatório" in response.get_json()["error"]


def test_update_rule_sucesso(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.rowcount = 1
    novo_rules = {"country": ["BR", "PT"]}
    mock_cursor.fetchone.return_value = {
        "flag_name": "flag-a",
        "is_enabled": False,
        "rules": novo_rules,
    }

    with mock_auth_ok(app_module):
        response = client.put(
            "/rules/flag-a",
            json={"rules": novo_rules, "is_enabled": False},
            headers={"Authorization": "Bearer chave-valida"},
        )

    assert response.status_code == 200
    assert response.get_json()["is_enabled"] is False

    # fields = ["rules = %s", "is_enabled = %s"] -> values = [Json(rules), is_enabled, flag_name]
    _, params = get_execute_params(mock_cursor)
    assert isinstance(params[0], app_module.Json)
    assert params[0].adapted == novo_rules
    assert params[1] is False
    assert params[2] == "flag-a"


def test_update_rule_nao_encontrada(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.rowcount = 0

    with mock_auth_ok(app_module):
        response = client.put(
            "/rules/nao-existe",
            json={"is_enabled": True},
            headers={"Authorization": "Bearer chave-valida"},
        )

    assert response.status_code == 404


# ----------------------------------------------------------------------
# DELETE /rules/<flag_name> (delete_rule)
# ----------------------------------------------------------------------


def test_delete_rule_sucesso(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.rowcount = 1

    with mock_auth_ok(app_module):
        response = client.delete(
            "/rules/flag-a", headers={"Authorization": "Bearer chave-valida"}
        )

    assert response.status_code == 204


def test_delete_rule_nao_encontrada(client, app_module, mock_db_conn):
    _, mock_cursor = mock_db_conn
    mock_cursor.rowcount = 0

    with mock_auth_ok(app_module):
        response = client.delete(
            "/rules/nao-existe", headers={"Authorization": "Bearer chave-valida"}
        )

    assert response.status_code == 404
