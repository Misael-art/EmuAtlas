# Instalação automatizada do ecossistema

Entrada única na raiz extraída:

```sh
./install.sh plan
./install.sh install --apply
./install.sh status
```

Sem argumentos, o script apenas planeja. Se Python estiver ausente, somente a execução com `--apply` tenta instalá-lo pelo gerenciador do host. As demais alterações seguem o grafo de `tools/distribution/config/installation.json`.

## Escopo da versão 0.2

- **Host:** pacotes nativos para Flatpak, Python/Pillow, arquivos compactados, mídia/PDF, Wine, MAME/chdman, bibliotecas de vídeo/fontes/janelas e compiladores/CMake/Ninja/Rust.
- **Apps:** Steam, RetroArch, Dolphin, Flycast, Azahar, melonDS, PCSX2, PPSSPP, Cemu e xemu via Flatpak por usuário. Instalações já disponíveis são reutilizadas; Steam nativo evita uma segunda instalação Flatpak.
- **Runtimes portáteis:** receitas existentes de DuckStation, RPCS3, shadPS4 e alternativas dos demais emuladores, com origem/tamanho/hash. As alternativas reutilizam o host quando configuráveis, em vez de baixar uma cópia indiscriminadamente.
- **Núcleos:** os 24 arquivos libretro distintos usados no perfil possuem receitas pinadas. URLs rolantes do buildbot não ignoram mudanças: outro hash bloqueia até uma revisão.
- **Frontends:** ES-DE adquirido por hash, Quiver preparado por seu instalador existente e RetroFE compilado de commit exato. A biblioteca pessoal de temas não acompanha a release; há um fallback textual próprio para uma instalação vazia.
- **Ferramentas:** venv local com versões declaradas de Pillow/amitools/py7zr/requests; wrappers de xdftool/xdfscan; ZArchive compilado com a política lossless de nível máximo; orbis-unpkg via Cargo com versão e lock upstream; Skraper pela origem pinada, com Wine do host.
- **Pesquisa:** Eden-NX2 possui receita de compilação para x86-64-v3; KytyPS5 e PS5PCEM têm aquisição pinada. Xenia Canary Linux usa pasta própria e não substitui a instalação Windows existente. Instalar esses protótipos não os homologa para gameplay ou venda.
- **Configuração:** disco virtual vazio oficial do xemu (preservando um disco do usuário existente), raízes canônicas, perfis/coleções/launchers e mídia por plataforma; configurações e atalhos do desktop/menu sem abrir GUIs. O perfil de uma instalação nova só é alterado automaticamente enquanto seu hash gerenciado continuar intacto.

O grafo tem 59 componentes, considerando grupos de pacotes e núcleos individuais. Os números podem mudar por release; o JSON é a seleção efetiva. Nenhum aplicativo sem relação com o ecossistema de emulação é instalado por inferência.

## Retomada e preservação

Cada execução grava plano, log por componente, resultados e conclusão em `outputs/installation/<run-id>/`. `latest.json` resume a última execução. Não há sucesso global se existir `failed` ou `blocked`: o processo retorna 2. Uma seleção parcial concluída usa `selected_scope_complete`; `all_components_installed` só é verdadeiro quando o grafo inteiro foi verificado. Tarefas independentes continuam; dependentes ficam bloqueadas. Repetir o comando consulta novamente o host, verifica hashes e reaproveita downloads íntegros; não confia apenas no resultado anterior.

Downloads usam cache por SHA-256 em `work/installation/downloads/`, TLS e hosts allowlist. Fontes/builds ficam em `work/installation/builds/`; intermediários pertencem a esse trabalho e não à biblioteca do cliente. Compilações têm dois jobs por padrão, máximo quatro, prazo por comando e encerramento apenas do grupo de processos criado pelo instalador. Com systemd de usuário disponível, usam unidade transitória limitada a 5 GiB, sem swap, 150% de CPU e prazo; a build pesada Eden exige esse controle e bloqueia se ele não estiver disponível. Use `--jobs 1` em hosts com pouca memória. Builds existentes válidas são preservadas; binários diferentes não são sobrescritos.

`./install.sh install --components zarchive --apply` seleciona um componente e suas dependências. Essa opção ainda pode instalar os pacotes do grupo nativo necessário. A instalação não desinstala o que já existe e não faz atualização geral do host. Backups dos perfis gerados e mudanças de registry ficam em `outputs/installation/backups/`. Remover aplicativos de host exige revisão própria; não há rollback automático que desinstale dependências compartilhadas.

## Privilégios, versões e limites

Arch/BigLinux usa pacman; Debian/Ubuntu usa apt-get; Fedora usa dnf. A matriz é explícita. O SO pode exigir uma autenticação administrativa via Polkit/sudo. Não há armazenamento de senha, adição automática de repositório arbitrário, desativação de assinaturas ou desbloqueio de SteamOS/hosts imutáveis. Nesses hosts, dependências nativas ausentes são bloqueios explícitos, ainda que aplicativos Flatpak possam funcionar.

Pacotes de distribuição e apps Flatpak resolvem versões do repositório no momento da instalação; commits Flatpak são registrados. Fontes de build e arquivos portáteis usam pins. Isso reproduz o procedimento e registra os resultados; não promete um sistema inteiro bit a bit idêntico entre distribuições, toolchains e datas. Dependências transitivas Python são registradas no relatório pip; ainda não constituem um wheelhouse universal offline.

O teste automatizado cobre grafo, falha/retomada, preservação, timeouts, extração e geração dos caminhos. A ferramenta ZArchive foi efetivamente compilada e passou round-trip completo. Isso não certifica que todas as distribuições, builds pesadas, codecs, assistentes de terceiros ou jogos já foram testados em host limpo. A release deve permanecer prévia de engenharia até essa matriz de aceitação ser concluída.

## Dados que pertencem ao usuário

Não baixar jogos, BIOS, firmware ou chaves por este fluxo. Não simular login Steam, credenciais de scraping, seleção de BIOS ou aceite de termos. Esses insumos/decisões são reportados no onboarding. O instalador não abre jogos e não modifica o perfil de controles desktop para testar uma instalação. O primeiro gameplay e a restauração física dos controles continuam uma aceitação própria.

Referências de implementação: [Flatpak](https://docs.flatpak.org/en/latest/using-flatpak.html), [RetroFE upstream](https://github.com/phulshof/RetroFE), [ZArchive](https://github.com/Exzap/ZArchive/tree/v0.1.2), [Xenia Canary](https://github.com/xenia-canary/xenia-canary/releases), [Skraper](https://www.skraper.net/).
