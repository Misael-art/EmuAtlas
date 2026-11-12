#!/usr/bin/env python3
"""Materialize the Aeon Nox composition system and its portable media view."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from html import escape as xml_escape
from pathlib import Path

SCRIPT = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT.parents[2]
LAYOUT_DIR = Path("layouts/Aeon Nox")
TEMPLATE_DIR = Path("Tools/materializers/templates")
NAVIGATION_STATE_REL = Path("Manifests/aeon-nox-navigation-state.json")
STATE_REL = Path("Manifests/aeon-nox-layouts-state.json")
STATE_VERSION = 1
A2_GAME_PREVIEW_FRAME = "../../Assets/shared/images/frames/bezel/color/aeon-nox-game-preview-frame.png"
COVER_PLACEHOLDER = "Assets/shared/images/previews/unavailable-media/dark/aeon-nox-quiet-cover-placeholder.png"
BASELINE_HASHES = {
    "layout.xml": "8e353e9461d2dcfa4e6bab790250c0e67c7c91e75baafa051b27ec4cfe04e3f9",
    "layout 4x3.xml": "43c8086c19380e41fc39d18ac5c72fa5a72ffcea2f1004e24549e248d857bea6",
}
FONT = "../../Assets/shared/fonts/families/redhatdisplay-regular.ttf"
FONT_MEDIUM = "../../Assets/shared/fonts/families/redhatdisplay-medium.ttf"
FONT_LIGHT = FONT
FONT_BOLD = "../../Assets/shared/fonts/families/redhatdisplay-bold.ttf"
AMBIENT = "../../Assets/shared/images/backgrounds/abstract/mode-geoblue.png"
AMBIENT_ALPHA = .48
SHADOW_GRADIENT = "../../Assets/shared/images/effects/shadowoverlay.png"
BLACK = "../../Assets/shared/images/backgrounds/black.png"
EDGE = "../../Assets/shared/images/ui/bar.png"
TEXT = "a9bdc9"
SECONDARY = "8199a8"
ACCENT = "94afbe"

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def attr(tag: str, name: str) -> str | None:
    match = re.search(rf'\b{name}="([^"]*)"', tag)
    return match.group(1) if match else None

def set_attr(tag: str, name: str, value: str) -> str:
    pattern = re.compile(rf'(\b{name}=")[^"]*(")')
    if pattern.search(tag):
        return pattern.sub(lambda match: match.group(1) + value + match.group(2), tag, count=1)
    return tag[:-2] + f' {name}="{value}"/>' if tag.endswith("/>") else tag[:-1] + f' {name}="{value}">'

def navigation_profile(collection: str, *, game_collection: bool = False) -> str:
    if game_collection:
        return "game"
    folded = collection.strip().lstrip("- ").casefold()
    return {
        "categorias": "category_hub",
        "coleções": "category_hub",
        "colecoes": "category_hub",
        "explorar plataformas": "category_hub",
        "menu collections": "category_hub",
        "marcas": "brands",
        "browse by manufacturer": "brands",
        "tipo de plataforma": "platform_type",
        "browse by platform type": "platform_type",
        "ano": "year",
        "browse by year": "year",
        "franquias": "franchise",
        "franchise collections": "franchise",
        "bits": "architecture",
        "browse by bits": "architecture",
        "favoritos": "favorites",
        "favorites": "favorites",
        "por plataforma": "favorites_by_platform",
        "favorites by platform": "favorites_by_platform",
        "options": "options",
        "opcoes": "options",
        "display options": "resolution",
        "resolucao": "resolution",
        "theme options": "theme",
        "tema": "theme",
        "controller options": "controls",
        "controles": "controls",
    }.get(folded, "category")

def navigation_context(collection: str, root: Path | None = None) -> str:
    """Describe the current B1/B2 route or the action offered by an option screen."""
    name = collection.strip()
    if name.startswith("- "):
        name = name[2:]
    folded = name.casefold()
    exact = {
        "coleções": "ESCOLHA FAVORITOS, PLATAFORMAS, FRANQUIAS OU OPCOES.",
        "categorias": "ESCOLHA UM FILTRO OU ABRA AS OPCOES.",
        "explorar plataformas": "ESCOLHA MARCA, TIPO, ANO OU ARQUITETURA.",
        "recentes": "PLATAFORMAS ABERTAS RECENTEMENTE; ENTER MOSTRA SEUS JOGOS.",
        "marcas": "ESCOLHA UMA MARCA PARA VER SUAS PLATAFORMAS.",
        "tipo de plataforma": "FILTRE PLATAFORMAS PELO TIPO DE DISPOSITIVO.",
        "ano": "ESCOLHA UM ANO PARA FILTRAR PLATAFORMAS.",
        "franquias": "ESCOLHA UMA FRANQUIA PARA ABRIR SEUS JOGOS.",
        "bits": "FILTRE PLATAFORMAS PELA ARQUITETURA DA CPU.",
        "favoritos": "ESCOLHA UMA PLATAFORMA PARA ABRIR SEUS JOGOS FAVORITOS.",
        "por plataforma": "ESCOLHA UMA PLATAFORMA PARA VER SEUS FAVORITOS.",
        "opcoes": "ESCOLHA TELA, TEMA OU CONTROLES DO FRONTEND.",
        "resolucao": "ESCOLHA A PROPORCAO ADEQUADA A SUA TELA.",
        "tema": "SELECIONE UM TEMA; O RETROFE REABRE NO MENU MAIN.",
        "controles": "ABRE O STEAM INPUT PARA CONFIGURAR DESKTOP E GAMEPAD.",
        "menu collections": "ESCOLHA UMA CATEGORIA PARA FILTRAR A BIBLIOTECA.",
        "favorites": "JOGOS FAVORITOS AGRUPADOS POR PLATAFORMA.",
        "favorites by platform": "ESCOLHA UMA PLATAFORMA PARA VER SEUS FAVORITOS.",
        "browse by manufacturer": "ESCOLHA UMA MARCA PARA VER SUAS PLATAFORMAS.",
        "browse by platform type": "FILTRE PLATAFORMAS PELO TIPO DE DISPOSITIVO.",
        "browse by year": "ESCOLHA UM ANO PARA FILTRAR PLATAFORMAS.",
        "franchise collections": "ESCOLHA UMA FRANQUIA PARA ABRIR SEUS JOGOS.",
        "browse by bits": "FILTRE PLATAFORMAS PELA ARQUITETURA DA CPU.",
        "options": "ESCOLHA TELA, TEMA OU CONTROLES DO FRONTEND.",
        "display options": "ESCOLHA A PROPORCAO QUE MELHOR COMBINA COM SUA TELA.",
        "theme options": "SELECIONE UM TEMA; O RETROFE REABRE NO MENU MAIN.",
        "controller options": "ABRE O STEAM INPUT PARA CONFIGURAR DESKTOP E GAMEPAD.",
    }
    if folded in exact:
        return exact[folded]
    if root is not None:
        parent_contexts = (
            ("Marcas", lambda value: f"PLATAFORMAS DA MARCA {value.upper()}."),
            ("Tipo de plataforma", lambda value: f"PLATAFORMAS CLASSIFICADAS COMO {value.upper()}."),
            ("Ano", lambda value: f"PLATAFORMAS COM PRIMEIRO LANCAMENTO EM {value.upper()}."),
            ("Bits", lambda value: f"PLATAFORMAS COM CPU DE {value.upper()}."),
            ("Franquias", lambda value: f"JOGOS DA FRANQUIA {value.upper()}."),
        )
        for parent, describe in parent_contexts:
            menu = root / "collections" / parent / "menu.txt"
            if menu.is_file():
                entries = {line.strip().casefold() for line in menu.read_text(encoding="utf-8", errors="replace").splitlines()}
                if folded in entries:
                    return describe(name)
    if folded.startswith("favoritos "):
        return f"JOGOS FAVORITOS DE {name[len('Favoritos '):].upper()}."
    for prefix, build in (
        ("manufacturer ", lambda value: f"PLATAFORMAS DA MARCA {value.upper()}."),
        ("platform type ", lambda value: f"PLATAFORMAS CLASSIFICADAS COMO {value.upper()}."),
        ("platform year ", lambda value: f"PLATAFORMAS COM PRIMEIRO LANCAMENTO EM {value.upper()}."),
        ("platform bits ", lambda value: f"PLATAFORMAS COM CPU DE {value.upper()} BITS."),
        ("collection ", lambda value: f"JOGOS DA FRANQUIA {value.upper()}."),
        ("favorite games ", lambda value: f"JOGOS FAVORITOS DE {value.upper()}."),
    ):
        if folded.startswith(prefix):
            return build(name[len(prefix):].strip())
    return "ESCOLHA UM ITEM PARA CONTINUAR; ESC VOLTA AO MENU ANTERIOR."

def alphabet_jump_feedback(width: int, height: int) -> str:
    """Show the native selected firstLetter during jumps, then quietly fade out.

    Jump events also cover page/random jumps in this engine, so the label says
    INDICE rather than promising that every jump changed the alphabetic group.
    The hold/fade belongs to onMenuIdle, not the blocking jump transition.
    """
    u = height / 1080
    x, y = width / 2, height * .47
    events = '''
        <onMenuJumpEnter><set duration=".06"><animate type="alpha" from="0" to="1"/></set></onMenuJumpEnter>
        <onMenuJumpExit><set duration=".01"><animate type="alpha" to="0"/></set></onMenuJumpExit>
        <onMenuIdle><set duration=".65"><animate type="xoffset" to="0"/></set><set duration=".28"><animate type="alpha" to="0" algorithm="easeoutsine"/></set></onMenuIdle>
        <onMenuExit><set duration=".06"><animate type="alpha" to="0"/></set></onMenuExit>
        <onGameEnter><set duration=".06"><animate type="alpha" to="0"/></set></onGameEnter>'''
    font = "../../Assets/shared/fonts/families/roboto-medium.ttf"
    letter_font = "../../Assets/shared/fonts/families/roboto-bold-0.ttf"
    return (
        '<!-- Native jump feedback: transient index, never a second menu. -->\n'
        f'    <image src="../../Assets/shared/images/backgrounds/black.png" x="{x}" y="{y}" width="{number(200*u)}" height="{number(170*u)}" xOrigin="center" yOrigin="center" alpha="0" layer="19">{events}</image>\n'
        f'    <text value="INDICE" font="{font}" fontSize="{number(20*u)}" loadFontSize="{number(20*u)}" fontColor="94afbe" x="{x}" y="{number(y-57*u)}" xOrigin="center" yOrigin="center" alpha="0" layer="19">{events}</text>\n'
        f'    <reloadableText type="firstLetter" menuScrollReload="true" textFormat="uppercase" font="{letter_font}" fontSize="{number(90*u)}" loadFontSize="{number(90*u)}" fontColor="a9bdc9" x="{x}" y="{number(y+4*u)}" xOrigin="center" yOrigin="center" alpha="0" layer="19">{events}</reloadableText>\n'
        f'    <text value="LB / RB   ·   PgUp / PgDn" font="{font}" fontSize="{number(16*u)}" loadFontSize="{number(16*u)}" fontColor="a5c6d8" x="{x}" y="{number(y+62*u)}" xOrigin="center" yOrigin="center" alpha="0" layer="19">{events}</text>\n'
    )

def collection_item_count(root: Path, collection: str) -> int:
    """Count direct generated menu/include/sub entries for a text override."""
    folder = root / "collections" / collection
    total = 0
    for name in ("menu.txt", "include.txt"):
        path = folder / name
        if path.is_file():
            total += sum(bool(line.strip()) for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
    for path in folder.glob("*.sub"):
        if path.is_file():
            total += sum(bool(line.strip()) for line in path.read_text(encoding="utf-8", errors="replace").splitlines())
    return max(1, total)

def load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"version": STATE_VERSION, "managed_hashes": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION or not isinstance(state.get("managed_hashes"), dict):
        raise ValueError(f"Formato de manifesto de estado incompatível: {path}")
    return state

def number(value: float) -> str:
    return f"{value:.3f}".rstrip('0').rstrip('.') if value else '0'


def scope_events(index: int, opacity: float = 1) -> str:
    a = number(opacity)
    return (
        f'<onMenuEnter menuIndex="{index}"><set duration=".20"><animate type="alpha" to="{a}" algorithm="easeoutsine"/></set></onMenuEnter>'
        f'<onMenuExit menuIndex="{index}"><set duration=".16"><animate type="alpha" to="0"/></set></onMenuExit>'
        f'<onGameEnter menuIndex="{index}"><set duration=".12"><animate type="alpha" to="0"/></set></onGameEnter>'
        f'<onGameExit menuIndex="{index}"><set duration=".20"><animate type="alpha" to="{a}"/></set></onGameExit>'
    )


def component(tag: str, attrs: dict, *, menu: int | None = None, opacity: float = 1, body: str = '') -> str:
    attrs = dict(attrs)
    if menu is not None:
        attrs['alpha'] = number(opacity if menu == 0 else 0)
        body = scope_events(menu, opacity) + body
    encoded = ' '.join(f'{k}="{xml_escape(number(v) if isinstance(v, (float, int)) else str(v), quote=True)}"' for k,v in attrs.items())
    return f'<{tag} {encoded}>' + body + f'</{tag}>' if body else f'<{tag} {encoded}/>'


def composition_geometry(width: int, height: int) -> dict:
    u = height / 1080
    content = min(width - 96*u, 2240*u)
    left = (width-content)/2
    footer_h = 116*u
    footer_top = height-12*u-footer_h
    ticker = (left, footer_top-58*u, content, 42*u)
    hero_h = 260*u
    preview_w = hero_h*4/3
    hero = (left, 78*u, content-preview_w-24*u, hero_h)
    preview = (left+content-preview_w, 78*u, preview_w, hero_h)
    band = (left, height*.435-58*u, content, 116*u)
    info_top = height*.55
    info = (left, info_top, content, ticker[1]-18*u-info_top)
    device = (left+16*u, info_top+16*u, content*.37-32*u, info[3]-32*u)
    card_bottom = ticker[1]-46*u
    card_height = 410*u
    slot = content/5
    card_width = min(card_height*5/7, slot*.84)
    game_preview_h = 270*u
    game_preview = (left+content-game_preview_h*4/3, 170*u, game_preview_h*4/3, game_preview_h)
    return {'width':width,'height':height,'unit':u,'left':left,'content':content,
            'footer':(left,footer_top,content,footer_h),'ticker':ticker,'hero':hero,
            'preview':preview,'band':band,'info':info,'device':device,
            'card_bottom':card_bottom,'card_height':card_height,'card_width':card_width,
            'slot':slot,'game_preview':game_preview}


def ambient_background(width: int, height: int) -> str:
    u=height/1080
    # Cover the canvas without distorting the geometry of the shared 16:9 art.
    art_width=max(width+64*u,(height+64*u)*16/9)
    art_height=art_width*9/16
    movement=(f'<onIdle><set duration="24"><animate type="xOffset" to="{number(10*u)}" algorithm="easeinoutsine"/><animate type="yOffset" to="{number(-6*u)}" algorithm="easeinoutsine"/></set>'
              '<set duration="24"><animate type="xOffset" to="0" algorithm="easeinoutsine"/><animate type="yOffset" to="0" algorithm="easeinoutsine"/></set></onIdle>')
    return component('image',{'src':BLACK,'x':0,'y':0,'width':width,'height':height,'layer':0})+component('image',{'id':10,'src':AMBIENT,'x':width/2,'y':height/2,'width':art_width,'height':art_height,'xOrigin':'center','yOrigin':'center','alpha':AMBIENT_ALPHA,'layer':1},body=movement)


def panel(box, index: int, *, layer: int = 3) -> str:
    x,y,w,h=box
    return component('image',{'src':EDGE,'x':x,'y':y,'width':w,'height':h,'layer':layer},menu=index,opacity=.42)+component('image',{'src':BLACK,'x':x+1.5,'y':y+1.5,'width':w-3,'height':h-3,'layer':layer+1},menu=index,opacity=.90)


def label(value: str, x, y, size, menu: int, *, width=None, color=SECONDARY, font=FONT, layer=12, identifier=None, center=False) -> str:
    attrs={'value':value,'x':x,'y':y,'font':font,'fontSize':size,'loadFontSize':size,'fontColor':color,'xOrigin':'center' if center else 'left','yOrigin':'center','layer':layer}
    if width is not None:attrs['maxWidth']=width
    if identifier is not None:attrs['id']=identifier
    return component('text',attrs,menu=menu)


def data(kind: str, x, y, size, menu: int, *, width=None, color=TEXT, font=FONT, layer=12, extra=None) -> str:
    attrs={'type':kind,'x':x,'y':y,'font':font,'fontSize':size,'loadFontSize':size,'fontColor':color,'xOrigin':'left','yOrigin':'center','layer':layer,'menuScrollReload':'true'}
    if width is not None:attrs['maxWidth']=width
    if extra:attrs.update(extra)
    return component('reloadableText',attrs,menu=menu)


def ticker_bar(box, menu: int, u: float, *, context: str | None = None) -> str:
    x,y,w,h=box
    surface=panel(box,menu,layer=13)
    if context is not None:
        return surface+label(context,x+18*u,y+h/2,24*u,menu,width=w-36*u,color=SECONDARY,layer=15)
    attrs={'id':400+menu,'type':'story','x':x+18*u,'y':y+h/2,'width':w-36*u,'height':34*u,'xOrigin':'left','yOrigin':'center','containerX':x+18*u,'containerY':y+4*u,'containerWidth':w-36*u,'containerHeight':h-8*u,'font':FONT,'fontSize':26*u,'loadFontSize':26*u,'fontColor':SECONDARY,'direction':'horizontal','scrollingSpeed':42*u,'startPosition':u,'startTime':'1.6','endTime':'1','menuScrollReload':'false','layer':15}
    return surface+component('reloadableScrollingText',attrs,menu=menu,opacity=.9)


def navigation_hints(width: int, height: int, *, orientation: str = 'vertical', profile: str = 'category') -> str:
    g=composition_geometry(width,height);u=g['unit'];x,y,w,h=g['footer']
    platform=[('Navegar','D-PAD','< / >'),('Abrir','A','Enter'),('Avancar','Y','F5'),('Letras','LB / RB','PgUp / PgDn'),('Opcoes','START','F10'),('Voltar','B','Esc')]
    games=[('Navegar','D-PAD','< / >'),('Jogar','A','Enter'),('Filtro','P / F1','Teclado'),('Favoritos','SELECT','F'),('Marcar','X','I / O'),('Letras','LB / RB','PgUp / PgDn'),('Opcoes','START','F10'),('Voltar','B','Esc')]
    actions={'game':'Jogar','resolution':'Aplicar','theme':'Trocar tema','controls':'Configurar','options':'Abrir'}
    lists=[('Navegar','D-PAD','< / >' if orientation=='horizontal' else 'Cima / Baixo'),(actions.get(profile,'Abrir'),'A','Enter'),('Opcoes','START','F10'),('Voltar','B','Esc')]
    profiles=[(0,platform),(1,games)] if profile=='root' else [(0,games if profile=='game' else lists)]
    out=[]
    for index,groups in profiles:
        out.append(panel(g['footer'],index,layer=16))
        step=w/len(groups)
        for i,(action,button,keyboard) in enumerate(groups):
            cx=x+(i+.5)*step;identifier=2000+index*100+i*10
            # Column centers stay fixed. Only the three text lines share the
            # requested alignment: left pairs / centered pair / left pairs.
            centered = i in {len(groups)//2-1,len(groups)//2}
            cap_w = min(156*u,step-24*u)
            tx = cx if centered else cx-cap_w/2+12*u
            # A framed keycap, medium-weight verb and quieter keyboard hint
            # share one anchor. The cap itself is drawn from shared surfaces.
            out.append(component('image',{'id':identifier,'src':EDGE,'x':cx,'y':y+30*u,'width':cap_w,'height':40*u,'xOrigin':'center','yOrigin':'center','layer':17},menu=index,opacity=.85))
            out.append(component('image',{'id':identifier+4,'src':BLACK,'x':cx,'y':y+30*u,'width':cap_w-2*u,'height':38*u,'xOrigin':'center','yOrigin':'center','layer':18},menu=index,opacity=.45))
            out.append(label(button,tx,y+30*u,30*u,index,color=TEXT,font=FONT_BOLD,layer=19,identifier=identifier+1,center=centered))
            out.append(label(action,tx,y+68*u,29*u,index,color=TEXT,font=FONT_MEDIUM,layer=19,identifier=identifier+2,center=centered))
            out.append(label(keyboard,tx,y+97*u,24*u,index,color=SECONDARY,font=FONT,layer=19,identifier=identifier+3,center=centered))
    return ''.join(out)


def preview(box, menu: int, u: float, *, identifier: int, opacity: float = 1) -> str:
    x,y,w,h=box;inset=6*u;clip=(x+inset,y+inset,w-2*inset,h-2*inset)
    cx,cy=x+w/2,y+h/2
    placeholder=component('image',{'src':'../../Assets/shared/images/previews/unavailable-media/dark/aeon-nox-game-preview-unavailable.png','x':x,'y':y,'width':w,'height':h,'layer':5},menu=menu,opacity=.62)
    video=component('reloadableVideo',{'id':identifier,'type':'video','imageType':'screenshot','x':cx,'y':cy,'width':clip[2],'minHeight':clip[3],'xOrigin':'center','yOrigin':'center','containerX':clip[0],'containerY':clip[1],'containerWidth':clip[2],'containerHeight':clip[3],'menuScrollReload':'true','layer':6},menu=menu,opacity=opacity)
    # Restore the optical glass finish inside the bezel, reusing one canonical
    # transparent asset; no duplicate video/decoder or extension below the box.
    glass=component('image',{'id':identifier+200,'src':'../../Assets/shared/images/glass/glass-highlight-2.png','x':clip[0],'y':clip[1],'width':clip[2],'height':clip[3],'containerX':clip[0],'containerY':clip[1],'containerWidth':clip[2],'containerHeight':clip[3],'layer':7},menu=menu,opacity=.10)
    frame=component('image',{'src':A2_GAME_PREVIEW_FRAME,'x':x,'y':y,'width':w,'height':h,'layer':8},menu=menu,opacity=.65)
    return placeholder+video+glass+frame


def device_echo(g) -> str:
    u=g['unit'];x,y,w,h=g['info']
    motion=(f'<onIdle><set duration="18"><animate type="xOffset" to="{number(12*u)}" algorithm="easeinoutsine"/><animate type="yOffset" to="{number(-5*u)}" algorithm="easeinoutsine"/></set>'
            '<set duration="18"><animate type="xOffset" to="0" algorithm="easeinoutsine"/><animate type="yOffset" to="0" algorithm="easeinoutsine"/></set></onIdle>')
    echo=component('reloadableImage',{'id':202,'type':'device','x':x+w*.60,'y':y+h*.52,'width':w*1.05,'xOrigin':'center','yOrigin':'center','containerX':x+2*u,'containerY':y+2*u,'containerWidth':w-4*u,'containerHeight':h-4*u,'menuScrollReload':'true','layer':5},menu=0,opacity=.32,body=motion)
    mask=component('image',{'id':203,'src':SHADOW_GRADIENT,'x':x+2*u,'y':y+2*u,'width':w-4*u,'height':h-4*u,'layer':6},menu=0,opacity=.7)
    return echo+mask


def logo_carousel(g) -> str:
    u=g['unit'];x,y,w,h=g['band'];step=w/5;cy=y+h/2
    defaults={'x':'center','y':cy,'maxWidth':step*.72,'height':74*u,'xOrigin':'center','yOrigin':'center','font':FONT_MEDIUM,'fontSize':34*u,'loadFontSize':34*u,'fontColor':ACCENT,'layer':10}
    items=[]
    for offset in range(-3,4):
        selected=offset==0;opacity=0 if selected or abs(offset)==3 else .32
        attrs={'xOffset':offset*step,'alpha':number(opacity)}
        if selected:attrs['selected']='true'
        items.append(component('item',attrs,body=scope_events(0,opacity)))
    menu=component('menu',{'menuIndex':0,'type':'custom','imageType':'logo_aeon_nox_light_white','orientation':'horizontal','scrollTime':'.22','scrollAcceleration':'.03','minScrollTime':'.12','x':0,'y':cy,'width':'stretch'},body=component('itemDefaults',defaults)+''.join(items))
    focus=component('reloadableImage',{'type':'logo_aeon_nox_white_outline','textFallback':'true','font':FONT_MEDIUM,'fontSize':34*u,'loadFontSize':34*u,'fontColor':ACCENT,'x':g['width']/2,'y':cy,'maxWidth':step*.72,'height':74*u,'xOrigin':'center','yOrigin':'center','menuScrollReload':'true','layer':11},menu=0,opacity=.80)
    return panel(g['band'],0,layer=7)+menu+focus


def cover_carousel(g) -> str:
    u=g['unit'];bottom=g['card_bottom'];step=g['slot'];w=g['card_width'];h=g['card_height']
    defaults={'x':'center','y':bottom,'width':w*.78,'maxHeight':h*.78,'xOrigin':'center','yOrigin':'bottom','font':FONT,'fontSize':18*u,'loadFontSize':18*u,'fontColor':SECONDARY,'layer':10}
    items=[]
    for offset in range(-3,4):
        selected=offset==0;opacity=.98 if selected else (.52 if abs(offset)<3 else 0)
        attrs={'xOffset':offset*step,'alpha':number(opacity)}
        if selected:attrs.update({'selected':'true','width':w,'maxHeight':h})
        items.append(component('item',attrs,body=scope_events(1,opacity)))
    menu=component('menu',{'menuIndex':1,'type':'custom','imageType':'cover','mode':'layout','textFallback':'false','orientation':'horizontal','scrollTime':'.24','scrollAcceleration':'.03','minScrollTime':'.12','x':0,'y':bottom,'width':'stretch'},body=component('itemDefaults',defaults)+''.join(items))
    names=[]
    for offset in range(-2,3):
        cx=g['width']/2+offset*step
        names.append(component('reloadableScrollingText',{'type':'title','selectedOffset':offset,'x':cx,'y':bottom+25*u,'width':step-20*u,'height':24*u,'xOrigin':'center','yOrigin':'center','alignment':'center','direction':'horizontal','scrollingSpeed':18*u,'startTime':5,'endTime':2,'font':FONT,'fontSize':18*u,'loadFontSize':18*u,'fontColor':TEXT if offset==0 else SECONDARY,'menuScrollReload':'true','layer':12},menu=1))
    return menu+''.join(names)


def platform_layout(width: int, height: int) -> str:
    g=composition_geometry(width,height);u=g['unit'];left=g['left'];c=g['content'];hero=g['hero'];out=[ambient_background(width,height)]
    # Stable header and independent A1/A2 regions, all using one uniform unit.
    out.append(label('AEON NOX',left,34*u,26*u,0,color=TEXT,font=FONT_BOLD))
    out.append(label('/ PLATAFORMAS',left+164*u,34*u,23*u,0,color=SECONDARY))
    out.append(data('collectionName',left,34*u,26*u,1,color=TEXT,font=FONT_BOLD,width=c*.65))
    for index in (0,1):
        out.append(data('time',left+c,34*u,21*u,index,color=SECONDARY,extra={'timeFormat':'%H:%M','xOrigin':'right'}))
    out.append(panel(hero,0))
    x,y,w,h=hero
    # The large preview is deliberately subordinate to identification.
    out.append(component('reloadableVideo',{'id':101,'type':'video','imageType':'screenshot','x':x+w/2,'y':y+h/2,'width':w,'minHeight':h,'xOrigin':'center','yOrigin':'center','containerX':x+2*u,'containerY':y+2*u,'containerWidth':w-4*u,'containerHeight':h-4*u,'menuScrollReload':'true','layer':5},menu=0,opacity=.5))
    out.append(component('image',{'src':BLACK,'x':x+2*u,'y':y+2*u,'width':w-4*u,'height':h-4*u,'layer':6},menu=0,opacity=.70))
    out.append(data('title',x+30*u,y+h*.43,44*u,0,width=w-60*u,color='b4c6d1',font=FONT_LIGHT,layer=9))
    out.append(data('manufacturer',x+32*u,y+h*.72,24*u,0,width=w*.6,color=SECONDARY,layer=9))
    out.append(data('year',x+w-32*u,y+h*.72,24*u,0,width=120*u,color=SECONDARY,layer=9,extra={'xOrigin':'right'}))
    out.append(preview(g['preview'],0,u,identifier=102))
    out.append(logo_carousel(g))
    out.append(panel(g['info'],0))
    out.append(device_echo(g))
    dx,dy,dw,dh=g['device']
    out.append(component('reloadableImage',{'id':201,'type':'device','x':dx+dw/2,'y':dy+dh/2,'maxWidth':dw-8*u,'maxHeight':dh-8*u,'xOrigin':'center','yOrigin':'center','containerX':dx,'containerY':dy,'containerWidth':dw,'containerHeight':dh,'menuScrollReload':'true','layer':8},menu=0,opacity=.96))
    info_x,info_y,info_w,info_h=g['info'];label_x=info_x+c*.40;value_x=label_x+158*u
    rows=[('FABRICANTE','manufacturer'),('TIPO','type'),('GERACAO','generation'),('ANO','year'),('MIDIA','media'),('CPU','cpu')]
    gap=(info_h-36*u)/6
    for i,(caption,kind) in enumerate(rows):
        ry=info_y+24*u+i*gap
        out.append(label(caption,label_x,ry,24*u,0,color=SECONDARY))
        out.append(data(kind,value_x,ry,27*u,0,width=left+c-value_x-24*u,color=TEXT))
    out.append(ticker_bar(g['ticker'],0,u))
    # A2: title and metadata stay calm; the artwork strip carries selection.
    pbox=g['game_preview'];text_w=pbox[0]-left-38*u
    out.append(data('title',left,105*u,40*u,1,width=c-66*u,color=TEXT,font=FONT_MEDIUM))
    out.append(component('reloadableImage',{'type':'isFavorite','mode':'commonlayout','x':left+c-18*u,'y':105*u,'width':34*u,'maxHeight':34*u,'xOrigin':'center','yOrigin':'center','menuScrollReload':'true','layer':12},menu=1,opacity=.78))
    out.append(label('LISTA ATUAL',left,156*u,18*u,1,color=SECONDARY))
    out.append(data('playlist',left+142*u,156*u,20*u,1,width=text_w-142*u,color=ACCENT))
    for i,kind in enumerate(('manufacturer','genre','year')):
        out.append(data(kind,left,212*u+i*42*u,24*u,1,width=text_w,color=SECONDARY))
    out.append(data('collectionIndexSize',left,354*u,22*u,1,width=text_w,color=SECONDARY))
    out.append(preview(pbox,1,u,identifier=103))
    out.append(cover_carousel(g))
    out.append(ticker_bar(g['ticker'],1,u))
    out.append(navigation_hints(width,height,orientation='horizontal',profile='root'))
    out.append(alphabet_jump_feedback(width,height))
    return '<layout width="'+str(width)+'" height="'+str(height)+'" font="'+FONT+'" loadFontSize="24" fontColor="'+TEXT+'">'+''.join(out)+'</layout>\n'


def text_navigation_layout(width: int, height: int, collection: str='', item_count: int=1, root: Path | None=None, *, menu_index: int=0, game_collection: bool=False) -> str:
    g=composition_geometry(width,height);u=g['unit'];left=g['left'];c=g['content'];profile=navigation_profile(collection,game_collection=game_collection)
    out=[ambient_background(width,height),data('collectionName',left,54*u,32*u,0,width=c*.78,color=TEXT),data('collectionIndexSize',left+c,54*u,23*u,0,color=SECONDARY,extra={'xOrigin':'right'})]
    list_w=c if game_collection else c*.58;list_x=left+list_w/2;row_h=64*u;rows=min(item_count,7);start=(g['ticker'][1]+100*u-rows*row_h)/2
    out.append(panel((left,start-30*u,list_w,60*u),0,layer=7))
    defaults={'x':list_x,'y':'center','width':list_w-36*u,'height':48*u,'spacing':16*u,'xOrigin':'center','yOrigin':'center','font':FONT,'fontSize':30*u,'loadFontSize':30*u,'fontColor':TEXT,'layer':10}
    out.append(component('menu',{'menuIndex':menu_index,'type':'vertical','orientation':'vertical','x':0,'y':start,'width':list_w,'height':(rows-.5)*row_h,'scrollTime':'.24','scrollAcceleration':'.03','minScrollTime':'.12'},body=component('itemDefaults',defaults)))
    if not game_collection:
        px=left+c*.64;pw=c*.36;py=height*.28;ph=270*u
        out.append(panel((px,py,pw,ph),0))
        out.append(label('SELECAO',px+24*u,py+40*u,20*u,0,color=SECONDARY))
        out.append(data('title',px+24*u,py+120*u,32*u,0,width=pw-48*u,color=TEXT,font=FONT_MEDIUM))
    out.append(ticker_bar(g['ticker'],0,u,context=navigation_context(collection,root)))
    out.append(navigation_hints(width,height,profile=profile))
    out.append(alphabet_jump_feedback(width,height))
    return '<layout width="'+str(width)+'" height="'+str(height)+'" font="'+FONT+'" loadFontSize="24" fontColor="'+TEXT+'">'+''.join(out)+'</layout>\n'


def build_outputs(root: Path) -> tuple[dict[Path, bytes], dict[str, object]]:
    source_paths = {
        "layout.xml": root / TEMPLATE_DIR / "aeon-nox-platform-16x9.xml",
        "layout 4x3.xml": root / TEMPLATE_DIR / "aeon-nox-platform-4x3.xml",
    }
    source_bytes = {name: path.read_bytes() for name, path in source_paths.items()}
    source_hashes = {name: digest(data) for name, data in source_bytes.items()}
    if any(source_hashes[name] != expected for name, expected in BASELINE_HASHES.items() if not (root / STATE_REL).exists()):
        mismatches = {name: source_hashes[name] for name, expected in BASELINE_HASHES.items() if source_hashes[name] != expected}
        raise ValueError(f"Template de plataforma mudou desde o diagnóstico; revisar antes de aplicar: {mismatches}")

    dimensions = {'layout.xml':(1920,1080),'layout 4x3.xml':(1600,1200),'layout 8x5.xml':(1920,1200),'layout 43x18.xml':(2580,1080),'layout 64x27.xml':(2560,1080),'layout 32x9.xml':(3840,1080)}
    platform_variants = {name:(platform_layout(w,h),w,h) for name,(w,h) in dimensions.items()}
    nav_state_path = root / NAVIGATION_STATE_REL
    nav_state = json.loads(nav_state_path.read_text(encoding="utf-8")) if nav_state_path.is_file() else {}
    nav_hashes = nav_state.get("managed_hashes", {}) if isinstance(nav_state, dict) else {}
    category_collections: set[str] = set()
    game_collections: set[str] = set()
    for rel_text in nav_hashes:
        parts = Path(rel_text).parts
        if len(parts) < 3 or parts[0] != "collections" or parts[1] == "Main":
            continue
        if parts[-1] in {"menu.txt", "include.txt"}:
            category_collections.add(parts[1])
        elif Path(parts[-1]).suffix.casefold() == ".sub":
            game_collections.add(parts[1])
    game_collections.difference_update(category_collections)

    outputs: dict[Path, bytes] = {}
    for name, (content, width, height) in platform_variants.items():
        outputs[LAYOUT_DIR / name] = content.encode("utf-8")
    for collection in sorted(category_collections, key=str.casefold):
        override_dir = LAYOUT_DIR / "collections" / collection / "layout"
        item_count = collection_item_count(root, collection)
        for name, (_platform_content, width, height) in platform_variants.items():
            override = text_navigation_layout(width, height, collection, item_count, root)
            outputs[override_dir / name] = override.encode("utf-8")

    # Generated .sub collections expose their merged game entries at menu
    # index 0. Give these B3 screens a single readable text row per game;
    # the root cover carousel repeats sparse, artwork-free lists and overlaps
    # long fallback titles. Runtime captures confirm index 1 renders a blank
    # page for these virtual collections.
    for collection in sorted(game_collections, key=str.casefold):
        override_dir = LAYOUT_DIR / "collections" / collection / "layout"
        item_count = collection_item_count(root, collection)
        for name, (_platform_content, width, height) in platform_variants.items():
            override = text_navigation_layout(
                width,
                height,
                collection,
                item_count,
                root,
                menu_index=0,
                game_collection=True,
            )
            outputs[override_dir / name] = override.encode("utf-8")

    summary = {
        "variants": {name: {"canvas": [width, height], "bytes": len(content.encode("utf-8"))} for name, (content, width, height) in platform_variants.items()},
        "navigation_menu_indices": {
            "A1_platforms_in_Main": 0,
            "A2_games_in_platform_collection": 1,
            "B1_and_generated_category_lists": 0,
            "B3_generated_sub_game_lists": 0,
        },
        "retrofe_ratio_variants": {
            "1280x800": "layout 8x5.xml",
            "3440x1440": "layout 43x18.xml",
            "2560x1080": "layout 64x27.xml",
            "5120x1440": "layout 32x9.xml",
        },
        "category_text_overrides": len(category_collections),
        "category_collections": sorted(category_collections, key=str.casefold),
        "game_text_overrides": len(game_collections),
        "game_collections": sorted(game_collections, key=str.casefold),
        "source_templates": {str(source_paths[name].relative_to(root)): source_hashes[name] for name in source_paths},
        "before_sha256": source_hashes,
    }
    return outputs, summary

def cover_presentation_aliases(root: Path) -> dict[str,str]:
    """One existing media payload per title plus one themed default per view."""
    aliases={};project=root.parent.parent.resolve();placeholder=(root/COVER_PLACEHOLDER).resolve()
    if not placeholder.is_file():raise ValueError('Canonical theme placeholder is missing')
    for collection in sorted((root/'collections').iterdir()):
        if not collection.is_dir() or collection.name.startswith('_'):continue
        folder=collection/'medium_artwork/cover';settings=collection/'settings.conf';physical=False
        if settings.is_file():
            match=re.search(r'^list\.path\s*=\s*([^#\r\n]+)',settings.read_text(errors='replace'),re.M)
            if match:
                raw=match.group(1).strip().replace('%BASE_ITEM_PATH%',str(project/'roms'))
                target=Path(raw) if Path(raw).is_absolute() else root/raw
                physical=target.resolve().is_relative_to(project/'roms') and target.is_dir()
        if not folder.is_dir() and not physical:continue
        view=LAYOUT_DIR/'collections'/collection.name/'medium_artwork/cover'
        default=view/'default.png';aliases[str(default)]=os.path.relpath(placeholder,(root/default).parent)
        if not folder.is_dir():continue
        for source in sorted(folder.iterdir()):
            if source.stem.casefold()=='default' or source.suffix.casefold() not in {'.png','.jpg','.jpeg','.webp'} or not source.is_file():continue
            payload=source.resolve()
            if not payload.is_relative_to(project/'media'):continue
            target=view/source.name;aliases[str(target)]=os.path.relpath(payload,(root/target).parent)
    return aliases


def cover_alias_change(root: Path, rel: str, literal: str, old_aliases: dict) -> tuple[dict, dict | None]:
    """Retarget only an unchanged managed link to the very same canonical payload."""
    target = root / rel
    change = {"path": rel, "action": "create-alias", "target": literal}
    if target.is_symlink():
        current = os.readlink(target)
        change["action"] = "unchanged"
        if current == literal:
            return change, None
        if old_aliases.get(rel) == current and not Path(current).is_absolute() and not Path(literal).is_absolute():
            try:
                project = root.parent.parent.resolve()
                before = target.resolve(strict=True)
                after = (target.parent / literal).resolve(strict=True)
                view = root / LAYOUT_DIR / "collections"
                if (target.parent.resolve().is_relative_to(view) and before == after
                        and before.is_relative_to(project / "media") and before.is_file()):
                    hasher = hashlib.sha256()
                    with before.open("rb") as stream:
                        while block := stream.read(1024 * 1024):
                            hasher.update(block)
                    change.update(action="update-managed-alias", target_before=current,
                                  payload=str(before.relative_to(project)), payload_sha256=hasher.hexdigest(),
                                  reason="Recorded managed alias and proposed alias resolve to the same canonical media file")
                    return change, None
            except (OSError, ValueError, RuntimeError):
                pass
        return change, {"path": rel, "reason": "cover presentation alias diverged; preserved"}
    if target.exists():
        change["action"] = "conflict"
        return change, {"path": rel, "reason": "cover presentation destination occupied; preserved"}
    return change, None


def update_managed_cover_alias(root: Path, change: dict) -> None:
    """Recheck provenance/payload and replace only the managed symlink atomically."""
    current, conflict = cover_alias_change(root, change["path"], change["target"],
                                          {change["path"]: change["target_before"]})
    if (conflict or current["action"] != "update-managed-alias"
            or current["payload_sha256"] != change["payload_sha256"]
            or current["payload"] != change["payload"]):
        raise ValueError(f"Cover alias or payload changed before update; preserved: {change['path']}")
    target = root / change["path"]
    temporary = target.parent / (".aeon-cover-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.symlink_to(change["target"])
        if not target.is_symlink() or os.readlink(target) != change["target_before"]:
            raise ValueError(f"Cover alias changed before replacement; preserved: {target}")
        os.replace(temporary, target)
    finally:
        if temporary.is_symlink() and os.readlink(temporary) == change["target"]:
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="raiz RetroFE; padrão relativo à ferramenta")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="aplica as variantes e registra os hashes")
    mode.add_argument("--check", action="store_true", help="retorna 1 se houver diferenças")
    parser.add_argument("--quiet", action="store_true", help="suprime a saída JSON")
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        outputs, summary = build_outputs(root)
        state_path = root / STATE_REL
        state = load_state(state_path)
        old_hashes = dict(state.get("managed_hashes", {}))
        conflicts = []
        changes = []
        aliases = cover_presentation_aliases(root)
        old_aliases = state.get("managed_aliases", {})
        for rel, literal in sorted(aliases.items()):
            change, conflict = cover_alias_change(root, rel, literal, old_aliases)
            if conflict:
                conflicts.append(conflict)
            changes.append(change)
        for rel, literal in sorted(old_aliases.items()):
            if rel not in aliases:
                target = root / rel
                if target.is_symlink() and os.readlink(target) == literal:
                    changes.append({"path": rel, "action": "remove-alias", "target": literal})
                elif target.exists() or target.is_symlink():
                    conflicts.append({"path": rel, "reason": "retired cover presentation diverged; preserved"})
        next_hashes = dict(old_hashes)
        expected_overrides = {str(path) for path in outputs}
        retired_layouts = [
            rel for rel in old_hashes
            if rel.startswith(str(LAYOUT_DIR) + "/") and rel not in expected_overrides
        ]
        for rel_text in sorted(retired_layouts):
            target = root / rel_text
            next_hashes.pop(rel_text, None)
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_file():
                conflicts.append({"path": rel_text, "reason": "retired generated layout is not a regular file; preserved"})
                continue
            before = target.read_bytes()
            before_hash = digest(before)
            if before_hash != old_hashes[rel_text]:
                conflicts.append({"path": rel_text, "reason": "retired generated layout changed since materialization; preserved"})
                continue
            changes.append({"path": rel_text, "action": "remove-managed", "sha256_before": before_hash, "sha256_after": None, "size_before": len(before)})
        for rel, data in sorted(outputs.items(), key=lambda item: str(item[0]).casefold()):
            target = root / rel
            if target.is_symlink() or (target.exists() and not target.is_file()):
                conflicts.append({"path": str(rel), "reason": "destination is symlink or not a regular file"})
                continue
            before = target.read_bytes() if target.is_file() else None
            before_hash = digest(before) if before is not None else None
            managed_hash = old_hashes.get(str(rel))
            is_original_base = rel in {LAYOUT_DIR / "layout.xml", LAYOUT_DIR / "layout 4x3.xml"} and before_hash == summary["before_sha256"].get(rel.name)
            if before is not None and before != data and managed_hash != before_hash and not is_original_base:
                conflicts.append({"path": str(rel), "reason": "existing content is not managed by this materializer"})
                continue
            after_hash = digest(data)
            next_hashes[str(rel)] = after_hash
            changes.append({"path": str(rel), "action": "create" if before is None else ("unchanged" if before == data else "update-managed"), "sha256_before": before_hash, "sha256_after": after_hash, "size_after": len(data)})

        state_payload = {"version": STATE_VERSION, "managed_hashes": next_hashes, "managed_aliases": aliases}
        state_bytes = (json.dumps(state_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        old_state = state_path.read_bytes() if state_path.is_file() else None
        changes.append({"path": str(STATE_REL), "action": "create" if old_state is None else ("unchanged" if old_state == state_bytes else "update-managed-state"), "sha256_before": digest(old_state) if old_state is not None else None, "sha256_after": digest(state_bytes), "size_after": len(state_bytes)})
        result = {"status": "conflict" if conflicts else "plan", "summary": summary, "changes": changes, "conflicts": conflicts}
        if not args.quiet:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if conflicts:
            if args.quiet:
                print(json.dumps({"status": "conflict", "conflict_count": len(conflicts),
                                  "conflicts": conflicts[:3],
                                  "diagnostic": "Run materialize-aeon-nox-layouts.py --check for the complete read-only diagnosis"},
                                 ensure_ascii=False), file=sys.stderr)
            return 2
        pending = any(item["action"] != "unchanged" for item in changes)
        if args.check:
            return 1 if pending else 0
        if args.apply:
            for change in changes:
                if change["action"] == "create-alias":
                    target = root / change["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() or target.is_symlink():
                        raise ValueError(f"Cover alias destination changed: {target}")
                    target.symlink_to(change["target"])
                elif change["action"] == "remove-alias":
                    target = root / change["path"]
                    if not target.is_symlink() or os.readlink(target) != change["target"]:
                        raise ValueError(f"Cover alias changed before removal: {target}")
                    target.unlink()
                elif change["action"] == "update-managed-alias":
                    update_managed_cover_alias(root, change)
            for change in changes:
                if change["action"] != "remove-managed":
                    continue
                target = root / str(change["path"])
                if target.is_symlink() or not target.is_file() or digest(target.read_bytes()) != change["sha256_before"]:
                    raise ValueError(f"Layout gerado mudou antes da remoção; preservado: {target}")
                target.unlink()
                try:
                    target.parent.rmdir()
                    target.parent.parent.rmdir()
                except OSError:
                    pass
            for rel, data in sorted(outputs.items(), key=lambda item: str(item[0]).casefold()):
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.read_bytes() != data:
                    target.write_bytes(data)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            if not state_path.is_file() or state_path.read_bytes() != state_bytes:
                state_path.write_bytes(state_bytes)
            if not args.quiet:
                print("APPLY_RESULT=written")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
