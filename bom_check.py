#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Проверка компонентов в BOM-файле по данным JLCPCB (EasyEDA/LCSC) API.

Тип компонента определяется по Designator (первая буква + цифра):
  - R1, R2, R_1, ... -> Resistor  (номинал, футпринт, парт-номер)
  - C1, C2, C_1, ... -> Capacitor (номинал, напряжение, футпринт, парт-номер)
  - L1, L2, L_1, ... -> Inductor  (номинал, ток, футпринт, парт-номер)
  - CELLS, TP1, D1, U1, ... -> только сверка Part (не R/C/L)
"""

import argparse, csv, os, re, sys, time
from dataclasses import dataclass, field
from typing import Optional
try: import requests
except ImportError: sys.exit("pip install requests")

JLC_API = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail"
EASYEDA_API = "https://easyeda.com/api/products/{code}/components"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36","Accept": "application/json, text/plain, */*"}

@dataclass
class ComponentInfo:
    code: str; found: bool = False; model: str = ""; spec: str = ""
    value: Optional[float] = None; value_raw: str = ""
    voltage: Optional[float] = None; voltage_raw: str = ""
    current: Optional[float] = None; current_raw: str = ""
    attributes: dict = field(default_factory=dict); error: str = ""

_session = requests.Session(); _session.headers.update(HEADERS)
_cache: dict[str, ComponentInfo] = {}

def _fetch_jlc(code):
    try:
        r = _session.get(JLC_API, params={"componentCode": code}, timeout=20)
        if r.status_code == 200:
            p = r.json()
            if p.get("code") == 200 and p.get("data"): return p["data"]
    except: pass
    return None

def _fetch_easyeda(code):
    try:
        r = _session.get(EASYEDA_API.format(code=code), timeout=20)
        if r.status_code == 200:
            p = r.json()
            if p.get("result"): return p["result"]
    except: pass
    return None

def _get_value_for_key(attrs, key_en):
    if not attrs: return None
    for a in attrs:
        en = a.get("attribute_name_en") or a.get("enName","")
        if en.lower() == key_en.lower(): return (a.get("attribute_value_name") or a.get("value",""))
        if en.lower() == f"{key_en}(v)": return (a.get("attribute_value_name") or a.get("value",""))
    return None

SUFFIX_MULT = {"m":1e-3,"u":1e-6,"n":1e-9,"p":1e-12,"k":1e3,"K":1e3,"M":1e6,"G":1e9}
CAP_SM = {"mF":1e-3,"uF":1e-6,"nF":1e-9,"pF":1e-12}
IND_SM = {"mH":1e-3,"uH":1e-6,"nH":1e-9}

def _strip(s): return re.sub(r"(?i)\s*(ohm|Ω|Ω|F|H|V|A|W)\s*$","",s).strip()
def pc(s):
    m = re.search(r"([\d.]+)\s*(m|u|n|p)?\s*F",s,re.I)
    if not m: return None
    return float(m.group(1)) * CAP_SM.get((m.group(2)or"u").lower()+"F",1e-6)
def pi(s):
    m = re.search(r"([\d.]+)\s*(m|u|n)?\s*H",s,re.I)
    if not m: return None
    return float(m.group(1)) * IND_SM.get((m.group(2)or"u").lower()+"H",1e-6)
def pr(s):
    ss = _strip(s)
    m = re.match(r"([\d.]+)\s*R\s*([\d.]*)$",ss,re.I)
    if m:
        a = float(m.group(1)or"0")
        return a + (float(f"0.{m.group(2)}") if m.group(2) else 0.0)
    m = re.match(r"([\d.]+)\s*(k|K|M|m|G)?\s*$",ss)
    if not m: return None
    return float(m.group(1)) * SUFFIX_MULT.get(m.group(2)or"",1.0)
def pv(s):
    m = re.search(r"([\d.]+)\s*(k|K|m)?\s*V",s,re.I)
    if not m: return None
    return float(m.group(1)) * {"k":1e3,"K":1e3,"m":1e-3}.get(m.group(2)or"",1.0)
def pci(s):
    m = re.search(r"([\d.]+)\s*(m|k)?\s*A",s,re.I)
    if not m: return None
    return float(m.group(1)) * {"m":1e-3,"k":1e3}.get(m.group(2)or"",1.0)
def vm(a,b,rt=0.1):
    if a==0 and b==0: return True
    if a==0 or b==0: return False
    return abs(a-b)/max(abs(a),abs(b))<=rt
def nt(c,k):
    if k=="cap": m=re.search(r"([\d.]+)\s*(m|u|n|p)?F",c,re.I)
    elif k=="ind": m=re.search(r"([\d.]+)\s*(m|u|n)?H",c,re.I)
    else: m=re.search(r"([\d.]+)\s*(k|K|M|m|G|R)?\s*(ohm)?",c,re.I)
    return m.group(0).strip() if m else c

SIZE_RE = re.compile(r"(?<![\d])(\d{4})(?![\d])")
def extract_size(t):
    if not t: return None
    m = SIZE_RE.search(t); return m.group(1) if m else None
def pfd(fp):
    d={}
    for k in ("BD","D","L","W"):
        m=re.search(rf"(?<!\w){k}([\d.]+)",fp,re.I)
        if m: d[k]=float(m.group(1))
    return d
def psd(s):
    d={}
    m=re.search(r"[Dd]([\d.]+)\s*x\s*[Ll]([\d.]+)",s)
    if m: d["D"]=float(m.group(1)); d["L"]=float(m.group(2))
    m=re.search(r"[Ww]([\d.]+)\s*mm",s)
    if m: d["W"]=float(m.group(1))
    return d

def norm_text(s):
    s = s.upper().strip(); return re.sub(r"[\s,/\-]+","",s)

def get_component(code):
    if code in _cache: return _cache[code]
    info = ComponentInfo(code=code)
    data = _fetch_jlc(code)
    if not data: data = _fetch_easyeda(code)
    if data:
        info.found = True
        info.model = data.get("componentModelEn") or data.get("productModel") or ""
        info.spec = data.get("componentSpecificationEn") or data.get("packageEn") or ""
        attrs = data.get("attributes") or data.get("componentAttributes") or []
        if isinstance(attrs, dict): attrs = attrs.get("attribute",[])
        if isinstance(attrs, str):
            try: import json; attrs = json.loads(attrs)
            except: attrs = []
        info.attributes = {a.get("enName",""):(a.get("attribute_value_name") or a.get("value","")) for a in attrs}
        for key in ("Capacitance","Resistance","Inductance"):
            v = _get_value_for_key(attrs,key)
            if v:
                info.value_raw = v
                if key=="Capacitance": info.value=pc(v)
                elif key=="Resistance": info.value=pr(v)
                elif key=="Inductance": info.value=pi(v)
                break
        vv = _get_value_for_key(attrs,"Voltage Rating")
        if vv: info.voltage_raw=vv; info.voltage=pv(vv)
        ii = _get_value_for_key(attrs,"Current Rating")
        if ii: info.current_raw=ii; info.current=pci(ii)
    else: info.error = "компонент не найден в JLCPCB/EasyEDA"
    _cache[code] = info
    return info

@dataclass
class RowResult:
    n:int; desc:str; designator:str; comment:str; footprint:str; part:str; jlc:str
    api_value:str=""; api_voltage:str=""; api_current:str=""; api_spec:str=""; api_model:str=""
    check_description:str=""; check_value:str=""; check_voltage:str=""
    check_current:str=""; check_footprint:str=""; check_part:str=""; status:str=""

def _tfd(d):
    d=d.upper().strip()
    if not d: return ""
    f=d[0]; r=d[1:].lstrip("_")
    if not r or not r[0].isdigit(): return ""
    if f=="R": return "resistor"
    if f=="C": return "capacitor"
    if f=="L": return "inductor"
    return ""

def check_row(n,row):
    desc=(row.get("Description")or"").strip()
    comment=(row.get("Comment")or"").strip()
    footprint=(row.get("Footprint")or"").strip()
    part=(row.get("Part")or"").strip()
    jlc=(row.get("JLCPCB Part")or"").strip()
    designator=(row.get("Designator")or"").strip().split(",")[0].strip()
    res=RowResult(n=n,desc=desc,designator=designator,comment=comment,footprint=footprint,part=part,jlc=jlc)
    comp=get_component(jlc)
    if not comp.found: res.status="NOT_FOUND";res.api_value=comp.error;return res
    res.api_value=comp.value_raw or str(comp.value)
    res.api_voltage=comp.voltage_raw or (f"{comp.voltage:g}V" if comp.voltage else "")
    res.api_current=comp.current_raw or (f"{comp.current:g}A" if comp.current else "")
    res.api_spec=comp.spec;res.api_model=comp.model
    ctype=_tfd(designator); is_cap=(ctype=="capacitor"); is_ind=(ctype=="inductor"); is_res=(ctype=="resistor")
    if ctype:
        dl=desc.lower()
        if ctype=="capacitor" and "capacitor" not in dl: res.check_description=f"FAIL (Description '{desc}' не соответствует Capacitor)"
        elif ctype=="resistor" and "resistor" not in dl: res.check_description=f"FAIL (Description '{desc}' не соответствует Resistor)"
        elif ctype=="inductor" and "inductor" not in dl: res.check_description=f"FAIL (Description '{desc}' не соответствует Inductor)"
        else: res.check_description="OK"
    else: res.check_description="SKIP (не R/C/L)"
    if is_cap: bv2=pc(comment)
    elif is_ind: bv2=pi(comment)
    elif is_res: bv2=pr(comment)
    else: bv2=None
    if not ctype: res.check_value="SKIP (не пассивный компонент)";res.check_footprint="SKIP (не пассивный компонент)"
    elif bv2 is None or comp.value is None: res.check_value="SKIP (не распознан номинал)"
    elif vm(bv2,comp.value): res.check_value="OK"
    else:
        kind2="cap" if is_cap else ("ind" if is_ind else "res")
        res.check_value=f"FAIL ({nt(comment,kind2)} != {comp.value_raw})"
    if is_cap:
        bv=pv(comment)
        if bv is None or comp.voltage is None: res.check_voltage="SKIP (не распознано напряжение)"
        elif vm(bv,comp.voltage,0.001): res.check_voltage="OK"
        else: res.check_voltage=f"FAIL ({bv:g}V != {comp.voltage_raw})"
    if is_ind:
        bi=pci(comment)
        if bi is None or comp.current is None: res.check_current="SKIP (не распознан ток)"
        elif vm(bi,comp.current,0.001): res.check_current="OK"
        else: res.check_current=f"FAIL ({bi:g}A != {comp.current_raw})"
    if ctype:
        bs=extract_size(footprint);as_=extract_size(comp.spec)
        if bs and as_:
            res.check_footprint="OK" if bs==as_ else f"FAIL (BOM {bs} != {comp.spec})"
        else:
            bd=pfd(footprint);ad=psd(comp.spec);notes=[];failed=False
            bd_=bd.get("BD")or bd.get("D");ad_=ad.get("D")
            if bd_ and ad_:
                if bd_==ad_: notes.append(f"D {bd_:g}={ad_:g}")
                else: failed=True;notes.append(f"D {bd_:g}!={ad_:g}")
            if not is_cap:
                bl=bd.get("L");al=ad.get("L")
                if bl and al:
                    if bl==al: notes.append(f"L {bl:g}={al:g}")
                    else: failed=True;notes.append(f"L {bl:g}!={al:g}")
            if is_ind:
                bw=bd.get("W");aw=ad.get("W")
                if bw and aw:
                    if bw==aw: notes.append(f"W {bw:g}={aw:g}")
                    else: failed=True;notes.append(f"W {bw:g}!={aw:g}")
            if notes: res.check_footprint=("FAIL (" if failed else "OK (")+", ".join(notes)+")"
            else: res.check_footprint="SKIP (нет типоразмера)"
    if not part: res.check_part="SKIP (нет Part)"
    else:
        bp=norm_text(part);im=norm_text(comp.model)
        if bp==im or (len(im)>=6 and im in bp) or (len(bp)>=6 and bp in im): res.check_part="OK"
        else: res.check_part=f"FAIL (BOM {part} != {comp.model})"
    if not ctype: checks=[res.check_part]
    else:
        checks=[res.check_description,res.check_value,res.check_footprint,res.check_part]
        if is_cap: checks.append(res.check_voltage)
        if is_ind: checks.append(res.check_current)
    if any(c.startswith("FAIL")for c in checks): res.status="FAIL"
    elif all(c.startswith("OK")for c in checks): res.status="PASS"
    else: res.status="PARTIAL"
    return res

def print_report(results):
    t=len(results);p=sum(r.status=="PASS" for r in results)
    f=sum(r.status=="FAIL" for r in results)
    pa=sum(r.status=="PARTIAL" for r in results)
    n=sum(r.status=="NOT_FOUND" for r in results)
    print("\n"+"="*100);print("РЕЗУЛЬТАТЫ ПРОВЕРКИ");print("="*100)
    for r in results:
        if r.status=="PASS": continue
        print(f"\n[{r.n}] {r.status}  {r.jlc}  {r.designator}")
        print(f"    Desc      : {r.desc}");print(f"    Comment   : {r.comment}")
        print(f"    Footprint : {r.footprint}");print(f"    Part      : {r.part}")
        ct=_tfd(r.designator);ap=[r.api_value]
        if ct=="capacitor": ap.append(f"voltage: {r.api_voltage}")
        if ct=="inductor": ap.append(f"current: {r.api_current}")
        ap.append(f"spec: {r.api_spec}");ap.append(f"model: {r.api_model}")
        print("    API value : "+"   ".join(ap))
        print(f"    -> desc   : {r.check_description}")
        print(f"    -> value  : {r.check_value}")
        if r.check_voltage: print(f"    -> voltage: {r.check_voltage}")
        if r.check_current: print(f"    -> current: {r.check_current}")
        print(f"    -> footprint: {r.check_footprint}")
        print(f"    -> part   : {r.check_part}")
    print("\n"+"-"*100);print(f"Всего строк проверено: {t}")
    print(f"  PASS     : {p}");print(f"  PARTIAL  : {pa}");print(f"  FAIL     : {f}")
    print(f"  NOT_FOUND: {n}");print("-"*100)

BOM_SUBDIR = os.path.join("doc","pcb")

def find_bom(root):
    if os.path.isfile(root): return root
    if not os.path.isdir(root): sys.exit(f"Папка не найдена: {root}")
    folder=os.path.join(root,BOM_SUBDIR)
    if not os.path.isdir(folder): sys.exit(f"Подпапка с BOM не найдена: {folder}")
    cand=[os.path.join(folder,n) for n in os.listdir(folder) if n.lower().startswith("bill of materials") and n.lower().endswith(".csv")]
    if not cand: sys.exit(f"В папке {folder} не найден BOM-файл")
    if len(cand)>1:
        cand.sort(key=os.path.getmtime,reverse=True)
        print("Найдено несколько BOM-файлов, беру самый свежий:")
        for c in cand: print(f"  - {os.path.basename(c)}")
    return cand[0]

def read_bom(path):
    for enc in ("utf-8-sig","cp1251","cp1252","latin-1"):
        try:
            with open(path,newline="",encoding=enc) as f: return list(csv.DictReader(f))
        except UnicodeDecodeError: continue
    sys.exit(f"Не удалось определить кодировку файла {path}")

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except: pass
    ap=argparse.ArgumentParser(description="Проверка BOM по JLCPCB API")
    ap.add_argument("root",help="корневая папка проекта")
    ap.add_argument("--delay",type=float,default=0.25,help="пауза между запросами, сек")
    args=ap.parse_args()
    bom_path=find_bom(args.root);print(f"BOM-файл: {bom_path}")
    try: rows=read_bom(bom_path)
    except FileNotFoundError: sys.exit(f"Файл не найден: {bom_path}")
    rows=[{(k or "").strip():v for k,v in row.items()} for row in rows]
    targets=[]
    for r in rows:
        des_raw=(r.get("Designator")or"").strip()
        des_first=des_raw.split(",")[0].strip()
        jlc=(r.get("JLCPCB Part")or"").strip()
        ct=_tfd(des_first)
        if ct and jlc: r["_type"]=ct;targets.append(r)
        elif jlc: r["_type"]="";targets.append(r)
    if not targets: sys.exit("Не найдено строк с JLCPCB Part для проверки")
    pc=sum(1 for r in targets if r.get("_type"))
    print(f"Найдено строк: {len(targets)} (пассивов R/C/L: {pc})")
    results=[];nt=len(targets);dn=len(str(nt))
    for i,row in enumerate(targets,start=1):
        jlc=(row.get("JLCPCB Part")or"").strip()
        pct=i/nt;bl=40;filled=int(bl*pct)
        bar="▓"*filled+"░"*(bl-filled)
        print(f"\r  {bar} {i:>{dn}}/{nt}  {jlc:<30}",end="")
        results.append(check_row(i,row))
        if jlc not in _cache or len(_cache)<2: time.sleep(args.delay)
    print()
    print_report(results)

if __name__=="__main__":
    main()
