import os
import sqlite3

from .sistema import CONTADORES, Atendimento, Medico, Paciente

ESQUEMA = """
CREATE TABLE IF NOT EXISTS medicos (
    id                INTEGER PRIMARY KEY,
    nome              TEXT    NOT NULL,
    registro          TEXT    NOT NULL,
    situacao          TEXT    NOT NULL CHECK (situacao IN ('Disponível', 'Em atendimento')),
    concluidos        INTEGER NOT NULL DEFAULT 0,
    cancelados        INTEGER NOT NULL DEFAULT 0,
    ultima_seq        INTEGER NOT NULL DEFAULT 0,
    ultima_atribuicao REAL,
    atendimento_id    INTEGER,
    removido          INTEGER NOT NULL DEFAULT 0 CHECK (removido IN (0, 1))
);

CREATE TABLE IF NOT EXISTS pacientes (
    id             INTEGER PRIMARY KEY,
    nome           TEXT    NOT NULL,
    chegada        REAL    NOT NULL,
    prioridade     INTEGER NOT NULL CHECK (prioridade BETWEEN 1 AND 5),
    situacao       TEXT    NOT NULL CHECK (situacao IN ('Aguardando', 'Em atendimento', 'Concluído', 'Cancelado')),
    medico_id      INTEGER REFERENCES medicos (id),
    atendimento_id INTEGER
);

CREATE TABLE IF NOT EXISTS atendimentos (
    id          INTEGER PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes (id),
    medico_id   INTEGER NOT NULL REFERENCES medicos (id),
    prioridade  INTEGER NOT NULL,
    inicio      REAL    NOT NULL,
    fim         REAL,
    situacao    TEXT    NOT NULL CHECK (situacao IN ('Em atendimento', 'Concluído', 'Cancelado')),
    resultado   TEXT
);

CREATE TABLE IF NOT EXISTS triagens (
    paciente_id INTEGER NOT NULL REFERENCES pacientes (id),
    ordem       INTEGER NOT NULL,
    momento     REAL    NOT NULL,
    de          INTEGER,
    para        INTEGER NOT NULL,
    PRIMARY KEY (paciente_id, ordem)
);

CREATE TABLE IF NOT EXISTS eventos (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    momento  REAL NOT NULL,
    mensagem TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS controle (
    chave TEXT PRIMARY KEY,
    valor INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_atendimentos_paciente ON atendimentos (paciente_id);
CREATE INDEX IF NOT EXISTS idx_atendimentos_medico   ON atendimentos (medico_id);
"""

COLUNAS_MEDICO = ("id", "nome", "registro", "situacao", "concluidos", "cancelados",
                  "ultima_seq", "ultima_atribuicao", "atendimento_id", "removido")
COLUNAS_PACIENTE = ("id", "nome", "chegada", "prioridade", "situacao", "medico_id", "atendimento_id")
COLUNAS_ATENDIMENTO = ("id", "paciente_id", "medico_id", "prioridade", "inicio", "fim", "situacao", "resultado")


def _upsert(tabela, colunas):
    lista = ", ".join(colunas)
    marcas = ", ".join("?" for _ in colunas)
    sets = ", ".join(f"{c} = excluded.{c}" for c in colunas if c != "id")
    return f"INSERT INTO {tabela} ({lista}) VALUES ({marcas}) ON CONFLICT(id) DO UPDATE SET {sets}"


def _ler_objeto(classe, colunas, linha):
    obj = classe.__new__(classe)
    for nome, valor in zip(colunas, linha):
        setattr(obj, nome, valor)
    return obj


def _linha(obj, colunas):
    return tuple(int(getattr(obj, c)) if c == "removido" else getattr(obj, c) for c in colunas)


class BancoDados:
    def __init__(self, caminho):
        self.caminho = caminho
        if caminho != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
        self.conexao = sqlite3.connect(caminho, check_same_thread=False)
        self.conexao.execute("PRAGMA foreign_keys = ON")
        self.conexao.executescript(ESQUEMA)

    def fechar(self):
        self.conexao.close()

    def vazio(self):
        c = self.conexao
        return (c.execute("SELECT COUNT(*) FROM controle").fetchone()[0] == 0
                and c.execute("SELECT COUNT(*) FROM medicos").fetchone()[0] == 0
                and c.execute("SELECT COUNT(*) FROM pacientes").fetchone()[0] == 0)

    def carregar(self, sistema):
        if self.vazio():
            return False
        c = self.conexao
        contadores = [1, 1, 1, 0]
        for chave, valor in c.execute("SELECT chave, valor FROM controle"):
            if chave in CONTADORES:
                contadores[CONTADORES.index(chave)] = valor

        medicos = []
        for linha in c.execute(f"SELECT {', '.join(COLUNAS_MEDICO)} FROM medicos ORDER BY id"):
            m = _ler_objeto(Medico, COLUNAS_MEDICO, linha)
            m.removido = bool(m.removido)
            medicos.append(m)

        pacientes = []
        for linha in c.execute(f"SELECT {', '.join(COLUNAS_PACIENTE)} FROM pacientes ORDER BY id").fetchall():
            p = _ler_objeto(Paciente, COLUNAS_PACIENTE, linha)
            p.atendimentos = [r[0] for r in c.execute(
                "SELECT id FROM atendimentos WHERE paciente_id = ? ORDER BY id", (p.id,))]
            p.triagens = [tuple(r) for r in c.execute(
                "SELECT momento, de, para FROM triagens WHERE paciente_id = ? ORDER BY ordem", (p.id,))]
            pacientes.append(p)

        atendimentos = [_ler_objeto(Atendimento, COLUNAS_ATENDIMENTO, linha) for linha in
                        c.execute(f"SELECT {', '.join(COLUNAS_ATENDIMENTO)} FROM atendimentos ORDER BY id")]
        eventos = [tuple(r) for r in c.execute("SELECT momento, mensagem FROM eventos ORDER BY id")]
        sistema.restaurar(medicos, pacientes, atendimentos, eventos, contadores)
        return True

    def salvar(self, sistema):
        medicos = sistema.listar_medicos(incluir_removidos=True)
        pacientes = sistema.listar_pacientes()
        atendimentos = sistema.listar_atendimentos()
        prox_paciente = sistema.proximo_paciente_id
        with self.conexao as c:
            c.execute("DELETE FROM triagens WHERE paciente_id >= ?", (prox_paciente,))
            c.execute("DELETE FROM atendimentos WHERE id >= ? OR paciente_id >= ?",
                      (sistema.proximo_atendimento_id, prox_paciente))

            c.executemany(_upsert("medicos", COLUNAS_MEDICO), [_linha(m, COLUNAS_MEDICO) for m in medicos])
            c.executemany(_upsert("pacientes", COLUNAS_PACIENTE), [_linha(p, COLUNAS_PACIENTE) for p in pacientes])
            c.executemany(_upsert("atendimentos", COLUNAS_ATENDIMENTO),
                          [_linha(a, COLUNAS_ATENDIMENTO) for a in atendimentos])
            for p in pacientes:
                c.execute("DELETE FROM triagens WHERE paciente_id = ? AND ordem >= ?", (p.id, len(p.triagens)))
                c.executemany(
                    "INSERT INTO triagens (paciente_id, ordem, momento, de, para) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(paciente_id, ordem) DO UPDATE SET momento = excluded.momento, "
                    "de = excluded.de, para = excluded.para",
                    [(p.id, i, t[0], t[1], t[2]) for i, t in enumerate(p.triagens)])

            c.execute("DELETE FROM pacientes WHERE id >= ?", (prox_paciente,))
            c.execute("DELETE FROM medicos WHERE id >= ?", (sistema.proximo_medico_id,))

            c.execute("DELETE FROM eventos")
            c.executemany("INSERT INTO eventos (momento, mensagem) VALUES (?, ?)", sistema.eventos)
            c.executemany("INSERT INTO controle (chave, valor) VALUES (?, ?) "
                          "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                          [(nome, getattr(sistema, nome)) for nome in CONTADORES])
