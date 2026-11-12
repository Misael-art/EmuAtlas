# Emuladores e dados portáteis

`emulator/` é a instalação canônica dos emuladores. O catálogo de plataformas continua em `frontend/RetroFE/Config/platform-catalog.json`; o perfil único permanece em `Config/profiles/host-linux.json` dentro do RetroFE. O nome histórico do perfil é preservado, e `tools/roms/portable_emulator.py` projeta seus comandos para o SO atual.

```text
emulator/
├── linux/<runtime_id>/       # AppImages, executáveis e núcleos .so
├── windows/<runtime_id>/     # EXEs, DLLs e dependências nativas
├── data/<runtime>/          # Dados únicos consumidos pelos dois hosts
├── launch.sh                # Entrada Linux
└── launch.cmd               # Entrada Windows com Python portátil local
```

As diferenças de configuração exigidas pelo SO ficam identificadas, como `retroarch-windows.cfg`, `cfg-windows` do MAME e `PPSSPP/windows/ppsspp.ini`. Saves, memory cards, estados, cheats, mods, packs gráficos, firmware instalado, caches e recursos compartilháveis continuam sob o mesmo runtime em `data/`. Cache usa o mesmo destino; o formato interno e a compatibilidade entre builds continuam sendo responsabilidade do emulador. Um state de uma versão não é certificado como carregável em outra.

| Conteúdo | Único destino ativo |
| --- | --- |
| Jogos | `roms/<system_id>/` |
| BIOS, fontes de firmware e chaves autorizadas | `bios/` |
| Mídia externa de jogos/plataformas | `media/<system_id>/<tipo>/` |
| Assets compartilhados da apresentação | `frontend/RetroFE/Assets/shared/` e `Assets/source-artwork/` |
| Dados de emulação | `emulator/data/<runtime>/` |
| Estado de cada frontend | Seu próprio `frontend/<nome>/UserData/` |
| Patches e documentação de variantes | `metadata/` e `mods/`, conforme o registry existente |

Firmware instalado no layout interno de um emulador, como `dev_flash`, permanece com seus dados. DLLs, plugins e recursos internos específicos de uma build permanecem com o programa. Recursos portáveis do RetroArch ficam em `data/RetroArch/{assets,database,info,overlays,shaders,autoconfig}`; os drivers de controle têm subpastas próprias dentro desse mesmo destino.

## Usar no Linux

```sh
./emulator/launch.sh --check
./emulator/launch.sh --system-id gba --item "roms/gba/Jogo.gba"
```

O host atual continua usando os Flatpaks já configurados. Sem eles, o launcher usa as builds portáteis locais declaradas nas receitas, quando disponíveis. `EMUATLAS_USE_PORTABLE=1` seleciona esse caminho explicitamente. MAME e dependências do sistema, drivers de GPU e FUSE ainda exigem suporte do host; esta organização não substitui o sistema operacional.

O launcher Linux do ES-DE recalcula seus caminhos e comandos antes de abrir a interface. Os scripts em `tools/roms/launchers/` encaminham um jogo canônico ao mesmo launcher; não carregam EmuDeck do home nem fazem sincronização externa.

## Preparar no Windows

Os pacotes Windows x64, 24 núcleos DLL, Python e 7-Zip ficam em `windows/`. Na biblioteca operacional organizada, esses componentes já foram adquiridos; a exportação pública contém somente fontes e receitas. Um checkout novo exige adquirir os pacotes antes de usar `launch.cmd`. A aquisição por receita pode ser preparada em Linux, por exemplo `python3 tools/roms/emutool.py install package --platform windows --runtime-id duckstation --apply`; Python, 7-Zip e os demais componentes têm suas receitas próprias no mesmo registry. Não se executa EXE por Wine automaticamente. Após conectar o volume preparado e fechar frontends/emuladores, abra um terminal na raiz:

```bat
emulator\launch.cmd --check
emulator\launch.cmd --prepare
emulator\launch.cmd --prepare --apply
emulator\launch.cmd --system-id gba --item "roms\gba\Jogo.gba"
```

`--prepare` mostra o plano. `--apply` cria somente os vínculos declarados em `windows_launch` nas receitas e projeta os comandos gerenciados do ES-DE, com preimagens, hashes e histórico oficial. Pastas pessoais preexistentes bloqueiam a preparação. Os vínculos são junctions NTFS, sem cópias do payload nem elevação administrativa. Ao mudar a letra da unidade ou a raiz, repita a preparação com os aplicativos fechados para recalculá-los.

**NTFS é necessário para os emuladores com diretórios fixos.** exFAT não oferece esses vínculos; esses runtimes ficam bloqueados. Não há formatação ou conversão automática de disco. A cópia/transporte precisa preservar os links relativos existentes de mídia e assets; a preparação do ES-DE valida essas referências e recusa uma árvore divergente. O filesystem do host atual é Btrfs e não é montável por um Windows padrão sem suporte adicional.

O RetroFE tem projeções em `frontend/RetroFE/launchers.windows/`, usando o Python local. Os executáveis nativos Windows dos frontends RetroFE/ES-DE ainda não estão instalados nesta biblioteca. A instalação Linux deles permanece canônica. O RocketLauncher continua preservado e sem ativação automática; sua integração tem gates próprios.

O Model 2 é o pacote local legado x86: jogos continuam em `roms/model2/roms/`, `CFG` e `NVDATA` em `data/Model2/`. Requer compatibilidade x86/DirectX 9 no Windows. Linux permanece bloqueado por falta de ponte validada. Esse pacote não tem receita de aquisição/licença de redistribuição comprovadas.

## Verificar e reverter

As receitas únicas em `tools/roms/config/portable-runtimes.json` registram origens, versões, tamanhos, hashes e contratos de dados. Pacotes foram testados integralmente como ZIP/7z e inventariados por SHA-256; cabeçalhos PE/ELF foram conferidos. Isso não comprova gameplay. A aceitação em Windows real, os controles e o carregamento dos saves/states compartilhados permanecem pendentes na lista global.

Migrações de diretórios/configuração usam `python3 tools/roms/emutool.py library verify|rollback --operation-id ID`; rollback exige `--apply`. Instalações têm seu rollback em `emutool install rollback`. Reveja o manifesto da operação antes de reverter e reverta primeiro as operações dependentes, incluindo os recursos compartilhados do RetroArch. Alteração posterior em arquivo protegido bloqueia rollback.

```bat
emulator\launch.cmd --rollback-frontends OPERATION_ID --apply
emulator\launch.cmd --rollback-bindings OPERATION_ID --apply
```

Esses rollbacks preservam dados e retiram somente vínculos gerenciados inalterados. Nenhuma operação apaga ROMs, saves, BIOS ou backups. O histórico e os inventários ficam em `outputs/roms-consolidacao-2026-10-24/operations/`; downloads verificados ficam em `work/installation/downloads/`. O diretório antigo de runtimes guarda apenas ZArchive, que é ferramenta de conversão, e resíduos vazios identificados no registro desta mudança.
