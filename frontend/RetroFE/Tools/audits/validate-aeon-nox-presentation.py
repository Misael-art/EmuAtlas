#!/usr/bin/env python3
"""Audit generated Aeon Nox composition and its nonduplicating cover view."""
from pathlib import Path
import argparse
import importlib.util
import json
import os
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def audit(root):
    spec = importlib.util.spec_from_file_location('aeon_layouts', root / 'Tools/materializers/materialize-aeon-nox-layouts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    outputs, summary = module.build_outputs(root)
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    for rel, data in outputs.items():
        path = root / rel
        require(path.is_file() and path.read_bytes() == data, f'Generated layout drift: {rel}')
        try:
            layout = ET.fromstring(data)
        except ET.ParseError as exc:
            errors.append(f'{rel}: {exc}')
            continue
        for e in layout.iter():
            for attribute in ('src', 'font'):
                value = e.get(attribute)
                if value and value.startswith('../../Assets/'):
                    require((root / module.LAYOUT_DIR / value).is_file(), f'Missing static asset: {rel} {value}')
        for e in layout:
            layer = e.get('layer')
            require(layer is None or 0 <= int(layer) <= 19, f'Unsupported layer: {rel} {layer}')
        if rel.parent != module.LAYOUT_DIR:
            continue
        width,height = float(layout.get('width')),float(layout.get('height'))
        g = module.composition_geometry(width,height)
        u = g['unit']
        def inside(inner,outer,tolerance=.02):
            x,y,w,h=inner;ox,oy,ow,oh=outer
            return x >= ox-tolerance and y >= oy-tolerance and x+w <= ox+ow+tolerance and y+h <= oy+oh+tolerance
        def viewport(element):
            return tuple(float(element.get(k)) for k in ('containerX','containerY','containerWidth','containerHeight'))
        canvas=(0,0,width,height)
        ambient=next(e for e in layout if e.get('id')=='10')
        aw,ah=float(ambient.get('width')),float(ambient.get('height'))
        require(abs(aw/ah-16/9)<.001,f'{rel}: abstract artwork distorted')
        require(aw>=width+60*u and ah>=height+60*u,f'{rel}: ambient movement could expose canvas edges')
        require(.4<=float(ambient.get('alpha'))<=.55,f'{rel}: ambient contrast budget drift')
        for name in ('hero','preview','band','info','device','game_preview','ticker','footer'):
            require(inside(g[name],canvas),f'{rel}: {name} escapes canvas')
        require(inside(g['device'],g['info']),f'{rel}: device escapes information panel')
        require(g['info'][1]+g['info'][3] < g['ticker'][1],f'{rel}: information overlaps ticker')
        require(g['ticker'][1]+g['ticker'][3] < g['footer'][1],f'{rel}: ticker overlaps footer')
        for identifier,region,index in ((101,'hero',0),(102,'preview',0),(103,'game_preview',1),(201,'device',0),(202,'info',0)):
            matches=[e for e in layout if e.get('id')==str(identifier)]
            require(len(matches)==1,f'{rel}: media {identifier} missing or repeated')
            if not matches: continue
            e=matches[0]
            require(inside(viewport(e),g[region],1.1),f'{rel}: media {identifier} escapes frame')
            for event_name in ('onMenuEnter','onMenuExit'):
                require(any(x.get('menuIndex')==str(index) for x in e.findall(event_name)),f'{rel}: media {identifier} has wrong scope')
            require(not any(x.get('type')=='minHeight' for x in e.iter('animate')),f'{rel}: unsupported minHeight tween')
        devices=[e for e in layout if e.tag=='reloadableImage' and e.get('type')=='device']
        require(sorted(e.get('id') for e in devices)==['201','202'],f'{rel}: foreground/echo device contract changed')
        foreground=next(e for e in devices if e.get('id')=='201')
        echo=next(e for e in devices if e.get('id')=='202')
        require(float(echo.get('alpha'))<float(foreground.get('alpha')) and int(echo.get('layer'))<int(foreground.get('layer')),f'{rel}: echo competes with foreground')
        require(echo.find('onIdle') is not None,f'{rel}: echo motion absent')
        for identifier in ('102','103'):
            video=next(e for e in layout if e.get('id')==identifier)
            glass=next(e for e in layout if e.get('id')==str(int(identifier)+200))
            require(viewport(glass)==viewport(video),f'{rel}: glass escapes the video viewport')
            require(int(video.get('layer'))<int(glass.get('layer'))<8,f'{rel}: glass must be between video and bezel')
            require(float(glass.get('alpha'))<=.15,f'{rel}: glass dominates the video')
        menus=[e for e in layout if e.tag=='menu']
        require(sorted(e.get('menuIndex') for e in menus)==['0','1'],f'{rel}: root menu contract changed')
        cover=next(e for e in menus if e.get('menuIndex')=='1')
        require(cover.get('mode')=='layout' and cover.get('textFallback')=='false',f'{rel}: native themed cover lookup missing')
        require(len(cover.findall('item'))==7,f'{rel}: carousel guard slots missing')
        for i,item in enumerate(cover.findall('item')):
            if 0<i<6:
                require(float(item.get('alpha','0'))>0,f'{rel}: cover disappears after scrolling resets point alpha')
        require(not any(e.tag=='image' and e.get('src','').endswith('/aeon-nox-quiet-cover-placeholder.png') for e in layout),f'{rel}: fixed placeholder reintroduced')
        captions=[e for e in layout if e.tag=='reloadableScrollingText' and e.get('type')=='title']
        require(sorted(int(e.get('selectedOffset')) for e in captions)==[-2,-1,0,1,2],f'{rel}: neighbor captions unsupported or missing')
        for index in (0,1):
            ticker=next((e for e in layout if e.get('id')==str(400+index)),None)
            require(ticker is not None and ticker.tag=='reloadableScrollingText',f'{rel}: lower description ticker absent')
            if ticker is not None:
                require(inside(viewport(ticker),g['ticker']),f'{rel}: description escapes ticker')
                require(ticker.get('fontColor')==module.SECONDARY,f'{rel}: description contrast role drift')
                require(float(ticker.get('startPosition','0'))>0,f'{rel}: native zero-position scrolling suppression reintroduced')
                require(ticker.get('menuScrollReload')=='false',f'{rel}: ticker would restart every frame after scrolling')
                require(abs(float(ticker.get('fontSize'))/u-26)<.01,f'{rel}: ticker text too small')
            labels=sorted((e for e in layout if e.tag=='text' and e.get('id') and 2000+100*index<=int(e.get('id'))<2100+100*index and int(e.get('id'))%10==2),key=lambda e:float(e.get('x')))
            require(len(labels)==(6 if index==0 else 8),f'{rel}: contextual action count changed')
            require(len({e.get('y') for e in labels})==1,f'{rel}: action baselines diverged')
            caps=sorted((e for e in layout if e.tag=='image' and e.get('id') and 2000+100*index<=int(e.get('id'))<2100+100*index and int(e.get('id'))%10==0),key=lambda e:float(e.get('x')))
            distances=[float(b.get('x'))-float(a.get('x')) for a,b in zip(caps,caps[1:])]
            require(not distances or max(distances)-min(distances)<.02,f'{rel}: control columns moved unevenly')
            for i,label in enumerate(labels):
                centered=i in {len(labels)//2-1,len(labels)//2}
                require(label.get('xOrigin')==('center' if centered else 'left'),f'{rel}: requested text alignment differs in column {i}')
            require(all(e.get('fontColor')==module.TEXT and e.get('font')==module.FONT_MEDIUM and abs(float(e.get('fontSize'))/u-29)<.01 for e in labels),f'{rel}: footer typography role drift')
        # One keycap anatomy across all controls; no unverified font pictograms.
        primary=[e for e in layout if e.get('id') and 2000<=int(e.get('id'))<2200 and int(e.get('id'))%10==1]
        require(all(e.tag=='text' and e.get('font')==module.FONT_BOLD for e in primary),f'{rel}: mixed glyph/text control families')
    expected = module.cover_presentation_aliases(root)
    state = json.loads((root / module.STATE_REL).read_text())
    require(expected == state.get('managed_aliases'), 'Cover alias state drift')
    for folder in (root / module.LAYOUT_DIR / 'collections').glob('*/medium_artwork/cover'):
        require(not folder.is_symlink(), f'Cover view directory must be managed: {folder}')
        for p in folder.iterdir():
            require(str(p.relative_to(root)) in expected, f'Unexpected file in cover presentation view: {p}')
    for rel, target in expected.items():
        p = root / rel
        require(p.is_symlink() and os.readlink(p) == target and p.is_file(), f'Broken/divergent cover alias: {rel}')
        require(not Path(target).is_absolute(), f'Absolute cover alias: {rel}')
        if p.name == 'default.png':
            require(p.resolve() == (root / module.COVER_PLACEHOLDER).resolve(), f'Wrong themed default: {rel}')
        else:
            require(p.stem.casefold() != 'default', f'Platform default reintroduced: {rel}')
            require(p.resolve().is_relative_to(root.parent.parent / 'media'), f'Cover outside canonical media: {rel}')
    return {'status': 'passed' if not errors else 'failed', 'layouts': len(outputs), 'aspect_variants': len(summary['variants']),
            'cover_aliases': len(expected), 'copied_media_payloads': 0, 'errors': errors,
            'scope': 'Static consistency, layer/geometry and alias audit; does not certify visual, input, sound or performance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--report', type=Path, help='Optional JSON report path')
    args = parser.parse_args()
    result = audit(args.root.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        args.report.write_text(text)
    print(text, end='')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
