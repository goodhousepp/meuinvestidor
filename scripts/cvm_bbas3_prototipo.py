#!/usr/bin/env python3
"""
Protótipo isolado CVM -> BBAS3 (Banco do Brasil S.A.)
Não altera o Meu Investidor nem o Cloudflare Worker.

Objetivo:
- baixar DFP anuais oficiais da CVM;
- localizar Banco do Brasil pelo código CVM 1023;
- extrair DRE e Balanço Patrimonial consolidados;
- preservar as linhas-fonte utilizadas;
- gerar JSON com lucro líquido e patrimônio líquido por ano.

Uso:
    python cvm_bbas3_prototipo.py
    python cvm_bbas3_prototipo.py --inicio 2021 --fim 2025
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional

CVM_CODE = "1023"
TICKER = "BBAS3"
COMPANY = "BANCO DO BRASIL S.A."
BASE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip"

# Nos arquivos DFP da CVM, as demonstrações consolidadas normalmente usam
# arquivos *_con_YYYY.csv.
DRE_MARKER = "_DRE_con_"
BPP_MARKER = "_BPP_con_"

# Contas padronizadas esperadas na DFP.
# Mantemos alternativas para não depender só de uma descrição textual.
NET_INCOME_CODES = (
    "3.11.01",  # Lucro/Prejuízo Consolidado do Período - atribuível aos controladores
    "3.11",     # Lucro/Prejuízo Consolidado do Período
)
EQUITY_CODES = (
    "2.03.01",  # Patrimônio Líquido - atribuível aos controladores (quando existente)
    "2.03",     # Patrimônio Líquido Consolidado
)

@dataclass
class SourceRow:
    year: int
    statement: str
    cd_conta: str
    ds_conta: str
    value: Decimal
    unit_scale: str
    dt_refer: str
    versao: str
    ordem_exerc: str
    file_name: str

    def to_dict(self):
        return {
            "ano": self.year,
            "demonstracao": self.statement,
            "codigo_conta": self.cd_conta,
            "descricao": self.ds_conta,
            "valor_cvm": str(self.value),
            "escala_moeda": self.unit_scale,
            "data_referencia": self.dt_refer,
            "versao": self.versao,
            "ordem_exercicio": self.ordem_exerc,
            "arquivo_fonte": self.file_name,
        }

def download_zip(year: int) -> bytes:
    url = BASE_URL.format(year=year)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MeuInvestidor-CVM-Prototype/1.0",
            "Accept": "application/zip,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()

def decode_csv(data: bytes) -> str:
    # Arquivos da CVM historicamente usam Windows-1252/Latin-1.
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    raise UnicodeDecodeError("cvm", data, 0, 1, "Encoding não reconhecido")

def decimal_pt(value: str) -> Decimal:
    # VL_CONTA costuma vir com ponto decimal no CSV, mas suportamos vírgula.
    s = (value or "").strip().replace(" ", "")
    if not s:
        raise InvalidOperation
    if "," in s and "." in s:
        # provável 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return Decimal(s)

def latest_company_rows(rows: List[dict]) -> List[dict]:
    rows = [r for r in rows if (r.get("CD_CVM") or "").strip() == CVM_CODE]
    if not rows:
        return []

    # Evita duplicação por reapresentação: usa a maior VERSAO disponível
    # para a data de referência mais recente daquele arquivo/ano.
    max_ref = max((r.get("DT_REFER") or "") for r in rows)
    rows = [r for r in rows if (r.get("DT_REFER") or "") == max_ref]

    def version_num(r):
        try:
            return int((r.get("VERSAO") or "0").strip())
        except ValueError:
            return 0

    max_version = max(version_num(r) for r in rows)
    rows = [r for r in rows if version_num(r) == max_version]

    # Para DRE, a CVM pode trazer exercício atual e anterior.
    # Preferimos o exercício atual ("ÚLTIMO"), se houver.
    current = [
        r for r in rows
        if (r.get("ORDEM_EXERC") or "").strip().upper() in ("ÚLTIMO", "ULTIMO")
    ]
    return current or rows

def select_account(rows: List[dict], codes: Iterable[str], year: int,
                   statement: str, file_name: str) -> Optional[SourceRow]:
    if not rows:
        return None

    # Primeiro por código exato, na ordem de preferência.
    for code in codes:
        matches = [r for r in rows if (r.get("CD_CONTA") or "").strip() == code]
        if matches:
            r = matches[0]
            try:
                value = decimal_pt(r.get("VL_CONTA", ""))
            except InvalidOperation:
                continue
            return SourceRow(
                year=year,
                statement=statement,
                cd_conta=code,
                ds_conta=(r.get("DS_CONTA") or "").strip(),
                value=value,
                unit_scale=(r.get("ESCALA_MOEDA") or "").strip(),
                dt_refer=(r.get("DT_REFER") or "").strip(),
                versao=(r.get("VERSAO") or "").strip(),
                ordem_exerc=(r.get("ORDEM_EXERC") or "").strip(),
                file_name=file_name,
            )
    return None

def parse_csv_member(zf: zipfile.ZipFile, file_name: str) -> List[dict]:
    text = decode_csv(zf.read(file_name))
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))

def find_member(names: List[str], marker: str, year: int) -> Optional[str]:
    suffix = f"{marker}{year}.csv".lower()
    for name in names:
        if name.lower().endswith(suffix):
            return name
    return None

def process_year(year: int) -> dict:
    blob = download_zip(year)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        dre_file = find_member(names, DRE_MARKER, year)
        bpp_file = find_member(names, BPP_MARKER, year)

        if not dre_file or not bpp_file:
            return {
                "ano": year,
                "ok": False,
                "erro": "Arquivos DRE/BPP consolidados não encontrados no ZIP.",
                "arquivos_encontrados": names[:20],
            }

        dre_rows = latest_company_rows(parse_csv_member(zf, dre_file))
        bpp_rows = latest_company_rows(parse_csv_member(zf, bpp_file))

        profit = select_account(
            dre_rows, NET_INCOME_CODES, year, "DRE", dre_file
        )
        equity = select_account(
            bpp_rows, EQUITY_CODES, year, "BPP", bpp_file
        )

        company_name = ""
        cnpj = ""
        for collection in (dre_rows, bpp_rows):
            if collection:
                company_name = (collection[0].get("DENOM_CIA") or "").strip()
                cnpj = (collection[0].get("CNPJ_CIA") or "").strip()
                break

        result = {
            "ano": year,
            "ok": bool(profit and equity),
            "ticker": TICKER,
            "codigo_cvm": CVM_CODE,
            "empresa": company_name or COMPANY,
            "cnpj": cnpj,
            "lucro_liquido": profit.to_dict() if profit else None,
            "patrimonio_liquido": equity.to_dict() if equity else None,
        }

        if profit and equity and equity.value != 0:
            # ROE simples = lucro anual / PL de fechamento.
            # Não tratamos isto como ROE final institucional; para esse cálculo
            # final, o ideal é PL médio (início/fim do exercício).
            result["roe_simples_percentual"] = str(
                (profit.value / equity.value * Decimal("100")).quantize(Decimal("0.01"))
            )
            result["observacao_roe"] = (
                "ROE simples usando PL de fechamento. "
                "O módulo definitivo deve preferir patrimônio líquido médio."
            )

        return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inicio", type=int, default=2021)
    parser.add_argument("--fim", type=int, default=2025)
    parser.add_argument(
        "--saida",
        default="cvm_bbas3_resultado.json",
        help="Arquivo JSON de saída",
    )
    args = parser.parse_args()

    if args.inicio > args.fim:
        parser.error("--inicio não pode ser maior que --fim")

    output = {
        "fonte": "CVM Dados Abertos - DFP",
        "ticker": TICKER,
        "codigo_cvm": CVM_CODE,
        "empresa_esperada": COMPANY,
        "periodo": {"inicio": args.inicio, "fim": args.fim},
        "resultados": [],
    }

    failures = 0
    for year in range(args.inicio, args.fim + 1):
        print(f"Baixando/processando DFP {year}...", file=sys.stderr)
        try:
            item = process_year(year)
        except Exception as exc:
            item = {
                "ano": year,
                "ok": False,
                "erro": f"{type(exc).__name__}: {exc}",
            }
        if not item.get("ok"):
            failures += 1
        output["resultados"].append(item)

    Path(args.saida).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\nResultado salvo em: {args.saida}", file=sys.stderr)

    # Código diferente de zero somente se todos os anos falharem.
    if failures == len(output["resultados"]):
        sys.exit(2)

if __name__ == "__main__":
    main()
