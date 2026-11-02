# Arquitetura e organização

O repositório contém código, receitas e documentação. Uma instalação acrescenta configurações locais, programas adquiridos na origem e os conteúdos do cliente. Builds são projeções geradas, nunca outra biblioteca de desenvolvimento.

| Diretório | Função | Versionar no produto |
| --- | --- | --- |
| tools/distribution/ | Bootstrap, diagnóstico, aquisição, exportação e testes | Sim |
| tools/roms/ | Organizadores e ferramentas existentes | Código e configurações revisadas |
| frontend/RetroFE/Tools/ | Materializadores, launchers e auditores | Código revisado |
| frontend/RetroFE/Config/ | Perfil e catálogo materializados dos templates | Templates; configuração final é local |
| frontend/RetroFE/Assets/shared/ | Arte comum única por tipo, categoria e estilo | Somente futura seleção com licença explícita |
| frontend/RetroFE/UserData/ | Dados graváveis de runtime | Não |
| frontend/ES-DE/ | Launcher e executável adquirido na origem | Código; não o executável |
| roms/<system_id>/ | Conteúdo fornecido pelo cliente | Não |
| media/<system_id>/<categoria>/ | Mídia externa única por jogo/plataforma | Não por padrão |
| bios/, saves/ | Dados do cliente e dependências restritas | Não |
| metadata/, mods/ | Identidade, variantes e patches locais | Não por padrão |
| outputs/ | Histórico, relatórios e releases geradas | Não |
| docs/business/ | Produto, processos e critérios de entrega | Sim |

## Configuração e portabilidade

`workspace.py` encontra a instalação pelo próprio arquivo. Os templates do catálogo/perfil usam caminhos relativos. `policy.json` e `roots.json` usam `${WORKSPACE}`, interpretado pelo `romtool` a cada leitura; mudar o nome ou a localização da pasta não mantém a raiz antiga autorizada. Configurações históricas absolutas continuam válidas na instalação original.

O bootstrap prepara somente uma pasta vazia ou previamente inicializada por ele. Não adota uma biblioteca existente sem marcador e não reescreve configurações alteradas pelo cliente. Para alterar a seleção de plataformas depois, revisar as configurações em vez de tratar uma divergência como autorização para apagar.

Os comandos que baixam usam um lock, origem HTTPS restrita, versão/tamanho/hash pinados, temporário no volume de destino e publicação sem sobrescrita. Um arquivo existente divergente bloqueia a operação. Nenhuma ferramenta de setup executa automaticamente os programas baixados.

A portabilidade da estrutura não torna Qt, Vulkan, Steam, drivers ou firmware independentes do host. Detectar essas dependências é parte do diagnóstico e da homologação. Não alterar perfis de controle durante o bootstrap.

## Reuso e dívida técnica

Não há segundo organizador: inventário, planos, conversão, mídia e rollback continuam nas ferramentas existentes. A nova camada trata instalação/distribuição: install.sh → setup.py → receitas e adaptadores existentes. Algumas ferramentas ainda usam o diretório histórico `outputs/roms-consolidacao-2026-10-24` e esperam catálogos/arte/templates de uma instalação RetroFE completa. Preservar essa compatibilidade nesta prévia; uma migração de estado exige projeto e testes próprios.

Exportar o código de um materializador não prova que todos os seus insumos foram distribuídos. O relatório comercial deve separar ferramenta disponível, insumos licenciados disponíveis e fluxo aceito em hardware. A release inicial homologa a criação vazia, a integridade do pacote e o download pinado; não homologa todos os comandos legados ou os temas.

## Orquestração do host

`setup.py` ordena dependências, instala pacotes nativos faltantes, reutiliza Flatpaks presentes, adquire fontes por hash/commit, limita builds a dois jobs por padrão e grava resultados por componente. Falhas ficam distintas de tarefas dependentes bloqueadas. A retomada revalida o estado real, não confia num marcador antigo de sucesso. Quiver e os demais launchers mantêm a implementação original; não há um segundo supervisor de gameplay.
