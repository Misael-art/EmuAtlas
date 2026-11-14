# Pegasus Frontend

O launcher usa a instalação única desta pasta e a biblioteca compartilhada do EmuAtlas.

```sh
./frontend/pegasus-frontend/launch.sh --check
./frontend/pegasus-frontend/launch.sh
```

## Pacote Linux

- Versão declarada `alpha16-82-gc3462e68`, release `weekly_2024w38` de 05/10/2024.
- Origem oficial `pegasus-fe_alpha16-82-gc3462e68_x11-static.zip`, 12.853.629 bytes.
- SHA-256 `f556d0ad36db77ffaf2c744ed575026c106378e377b4d8643e0f5a87634fd55b`, calculado após aquisição TLS da release oficial. A API dessa release fornece tamanho e origem.
- Licença upstream GPL-3.0-or-later, com avisos em `UPSTREAM-LICENSE.md` e orientações em `UPSTREAM-README.md`.
- A receita `pegasus` do instalador verifica o pacote, extrai pelo executor existente, protege destinos e registra o recibo de todos os membros publicados. A seleção pública distribui a receita e o launcher.

[Release upstream](https://github.com/mmatyas/pegasus-frontend/releases/tag/weekly_2024w38)

## Dados e mídia

O modo nativo `--portable` usa `config/` para configurações, diretórios de jogos, metadados gerados e estatísticas. Temas instalados são recursos de runtime em `themes/`. O valor `general.theme` é relativo a `config/`, portanto um tema local nessa raiz usa `../themes/<tema>`. O cache XDG é direcionado para `config/cache/` somente no processo do frontend. O pacote Linux requer X11/XWayland e usa o plugin Qt `xcb`. [Modo portátil](https://pegasus-frontend.org/docs/user-guide/portable/), [diretórios upstream](https://pegasus-frontend.org/docs/user-guide/config-dirs/)

A migração selecionada dos dados legados usa `emutool library` com inventário, hashes, preimagens e verificação. O plano do consumidor oferece `consumer-plan --frontend pegasus --portable-runtime`, seguido de apply/verify/rollback pelo mesmo materializador de mídia. Ele preserva os comandos pessoais e encaminha comandos legados reconhecidos ao perfil nativo único. As variáveis `EMUATLAS_ROOT` e `EMUATLAS_PYTHON` são calculadas pelo launcher, que mantém a pasta do programa como diretório de trabalho.

ROMs vêm de `roms/<system_id>/` e imagens de `media/<system_id>/<tipo>/`. A projeção `config/metafiles/canonical-media.metadata.pegasus.txt` reúne referências relativas, entradas executáveis e exclusões de dependências CUE/M3U. Referências gerenciadas ausentes são documentadas e retiradas pelo plano, com reversão disponível. [Metadados e variáveis de execução](https://pegasus-frontend.org/docs/user-guide/meta-files/)

A entrada padrão usa teclado e gamepads nativos. `RETROFE_INPUT_BACKEND=steam` seleciona a integração opcional compartilhada. O início valida recibo, bibliotecas, raiz dos dados e referências existentes. `--check` informa essa condição estrutural. O aceite de navegação, jogo, troca de disco e retorno segue a matriz de validação.
