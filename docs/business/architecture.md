# Arquitetura e organização

O repositório contém código, receitas e documentação. Uma instalação acrescenta configurações locais, programas adquiridos na origem e os conteúdos do cliente. Builds são projeções geradas, nunca outra biblioteca de desenvolvimento.

| Diretório | Função | Versionar no produto |
| --- | --- | --- |
| tools/distribution/ | Bootstrap, diagnóstico, aquisição, exportação e testes | Sim |
| tools/roms/ | Organizadores e ferramentas existentes | Código e configurações revisadas |
| frontend/RetroFE/Tools/ | Materializadores, launchers e auditores | Código revisado |
| frontend/RetroFE/Config/ | Perfil e catálogo materializados dos templates | Templates; configuração final é local |
| frontend/RetroFE/Assets/shared/ | Assets canônicos compartilhados por toda a estrutura, por tipo, categoria e estilo | Somente futura seleção com licença explícita |
| frontend/RetroFE/Assets/source-artwork/ | Fontes editáveis compartilhadas com os fluxos de criação | Somente futura seleção com licença explícita |
| frontend/RetroFE/UserData/ | Estado próprio do frontend | Não |
| emulator/linux/, emulator/windows/ | Binários e dependências nativas por SO | Somente launchers revisados; binários fora da release |
| emulator/data/ | Dados únicos de emuladores e recursos portáveis | Não |
| frontend/pegasus-frontend/ | Runtime adquirido, launcher e dados portáteis em config/ | Código e receita, com jogos e mídia nas raízes compartilhadas |
| frontend/ES-DE/ | Launcher e executável adquirido na origem | Código; não o executável |
| frontend/RocketLauncher/ | Launcher alternativo de jogos, com módulos, plugins e configuração locais | Pacote local do usuário; fora do instalador/release |
| roms/<system_id>/ | Conteúdo fornecido pelo cliente | Não |
| media/<system_id>/<categoria>/ | Mídia externa única por jogo/plataforma | Não por padrão |
| bios/, saves/ | Dados do cliente e dependências restritas | Não |
| metadata/, mods/ | Identidade, variantes e patches locais | Não por padrão |
| outputs/ | Histórico, relatórios e releases geradas | Não |
| docs/business/ | Produto, processos e critérios de entrega | Sim |

## Configuração e portabilidade

Os emuladores e núcleos são adquiridos pelas receitas existentes em `tools/roms/config/portable-runtimes.json` e instalados em `emulator/<SO>/`. O launcher comum deriva os comandos do perfil único e mantém `emulator/data/<runtime>/` compartilhado. Saves, cartões, states, cache, mods, cheats, packs gráficos e recursos independentes de SO não ganham cópias por host. Pastas fixas Windows usam junctions NTFS gerenciadas, revalidadas ao trocar de letra/raiz; arquivos pessoais existentes bloqueiam a operação. exFAT não atende esses vínculos. Origem, limites, preparação e rollback em [emulator/README.md](../../emulator/README.md). A aceitação Windows nativa e os executáveis Windows dos frontends continuam pendentes.

As duas bibliotecas em `frontend/RetroFE/Assets/` atendem temas, emuladores, frontends e ferramentas de criação por links simbólicos relativos gerenciados ou apontamentos nas configurações. A localização física permanece canônica e não implica uso exclusivo pelo RetroFE. Cada consumidor usa o mesmo payload e comprova formato/base de resolução dos caminhos; absolutos necessários são derivados da raiz atual. Catálogo/índice continuam únicos. A decisão de 11/11/2026 substitui a proposta de migração para uma raiz `assets/` separada; mídia de jogos continua em `media/<system_id>/<categoria>/`.

`workspace.py` encontra a instalação pelo próprio arquivo. Os templates do catálogo/perfil usam caminhos relativos. `policy.json` e `roots.json` usam `${WORKSPACE}`, interpretado pelo `romtool` a cada leitura; mudar o nome ou a localização da pasta não mantém a raiz antiga autorizada. Configurações históricas absolutas continuam válidas na instalação original.

O bootstrap prepara somente uma pasta vazia ou previamente inicializada por ele. Não adota uma biblioteca existente sem marcador e não reescreve configurações alteradas pelo cliente. Para alterar a seleção de plataformas depois, revisar as configurações em vez de tratar uma divergência como autorização para apagar.

Os comandos que baixam usam um lock, origem HTTPS restrita, versão/tamanho/hash pinados, temporário no volume de destino e publicação sem sobrescrita. Um arquivo existente divergente bloqueia a operação. Nenhuma ferramenta de setup executa automaticamente os programas baixados.

A portabilidade da estrutura não torna Qt, Vulkan, Steam, drivers ou firmware independentes do host. Detectar essas dependências é parte do diagnóstico e da homologação. Não alterar perfis de controle durante o bootstrap.

O RocketLauncher está cadastrado como alternativa ao launcher host dos frontends. A instalação local contém PE32 Windows e plugin RetroFE; os caminhos Windows antigos e a compatibilidade com o host Linux ainda exigem validação. ES-DE, Pegasus e Quiver não recebem suporte presumido pela presença do pacote. O cadastro protege suas referências sem trocar os perfis ativos, acrescentar componente ao instalador ou criar outra biblioteca. Quando o pacote local estiver instalado, seu README registra os detalhes e a proveniência; esse conteúdo não integra a exportação pública.

## Entrada móvel de sessão

O painel compartilhado usa o mapeamento visual `frontend/RetroFE/Config/controller-artwork.json`, com `player_capacity=8` como padrão. O arquivo único de preferências em UserData conserva a ordem e as escolhas visuais. A leitura amplia preferências antigas de quatro portas em memória.

`tools/roms/mobile_controller.py` atende os três arquivos explícitos da página responsiva por HTTP na rede local. A ação Adicionar inicia um processo por raiz/sessão do usuário, com descritor e segredo administrativos privados em XDG_RUNTIME_DIR. O QR Code carrega um convite de dois minutos no fragmento do endereço. Cada navegador recebe um token independente. As mensagens contêm snapshots completos e sequenciados, e o host neutraliza a origem após 750 ms sem atualização.

O processo cria origens evdev/uinput próprias. O roteador existente aplica a atribuição às portas virtuais estáveis. Frontend e jogo mantêm leases locais e informam foco, preservando o pareamento durante o lançamento. Solicitações de jogador vindas da página são confirmadas pelo consumidor focado através da mesma gravação durável usada pelo arraste. A ausência de consumidores encerra o serviço e remove os dispositivos. O backend de entrada atual usa Linux/KDE. O navegador do celular ou tablet consome apenas HTML, CSS e JavaScript locais.

## Jornada de saves de sessão

O launcher RetroArch transmite ao supervisor o contexto relativo da plataforma, ROM, conteúdo efetivamente lançado, core e configuração. `save_journey.py` acompanha apenas esse processo pelo stdin privado e pelo diretório nativo. O ciclo de vida integra o roteador existente. A UI `save_journey_ui.py` reutiliza a instância Tk do feedback de controles e o mesmo bloqueio de entrada enquanto a lista está aberta.

`emulator/data/RetroArch/states/journey/<game_id>/` reúne SQLite, versões nativas por SHA-256, PNGs com metadados imutáveis e perfis efêmeros. A identidade normaliza a extensão de core entre SOs. Favoritos, tags, notas, sequência e relações são transacionais. A exportação CSV constitui uma projeção no histórico oficial da sessão.

A retomada preserva o momento corrente, prepara uma cópia nativa em slot temporário alto e confirma a execução antes da transição visual. Um registro durável permite reconciliar cópias derivadas após interrupção. A comparação do core e do conteúdo protege a seleção. A compatibilidade efetiva segue dependente do core e da aceitação no host. O backend possui caminho Windows e o overlay atual usa Linux/KDE. O contrato detalha os limites de tempo observado, flush de SRAM, imagens e validação. [Jornada de saves](../../tools/roms/docs/save-journey.md)

## Reuso e dívida técnica

Não há segundo organizador: inventário, planos, conversão, mídia e rollback continuam nas ferramentas existentes. A nova camada trata instalação/distribuição: install.sh → setup.py → receitas e adaptadores existentes. Algumas ferramentas ainda usam o diretório histórico `outputs/roms-consolidacao-2026-10-24` e esperam catálogos/arte/templates de uma instalação RetroFE completa. Preservar essa compatibilidade nesta prévia; uma migração de estado exige projeto e testes próprios.

Exportar o código de um materializador não prova que todos os seus insumos foram distribuídos. O relatório comercial deve separar ferramenta disponível, insumos licenciados disponíveis e fluxo aceito em hardware. A release inicial homologa a criação vazia, a integridade do pacote e o download pinado; não homologa todos os comandos legados ou os temas.

## Orquestração do host

`setup.py` ordena dependências, instala pacotes nativos faltantes, reutiliza Flatpaks presentes, adquire fontes por hash/commit, limita builds a dois jobs por padrão e grava resultados por componente. Falhas ficam distintas de tarefas dependentes bloqueadas. A retomada revalida o estado real, não confia num marcador antigo de sucesso. Quiver e os demais launchers mantêm a implementação original; não há um segundo supervisor de gameplay.
