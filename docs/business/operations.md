# Instalação, fontes e mídia

## Instalação automatizada

O fluxo principal é `./install.sh plan`, `./install.sh install --apply` e `./install.sh status`. Consulte [instalação automatizada](automated-install.md) para componentes, privilégios, retomada e limites. O roteiro abaixo descreve a criação mínima, útil também para diagnóstico.

## Estrutura mínima

1. Verificar o SHA-256 do arquivo entregue pelo canal privado e conferir a release antes de inicializar.
2. Extrair numa pasta exclusiva em Linux x86_64, com Python >= 3.10.
3. Executar `python3 tools/distribution/release.py verify --root .` na raiz extraída, antes de criar userdata.
4. Executar `python3 tools/distribution/workspace.py doctor`.
5. Revisar `workspace.py plan --systems mastersystem nes` e aplicar com `workspace.py init --systems mastersystem nes --apply`.
6. Adquirir somente os componentes selecionados; a inicialização não dispara downloads nem instalações do sistema.
7. Importar o conteúdo do cliente para as raízes canônicas por fluxo revisado; validar caminhos, integridade e dependências.
8. Testar as jornadas e entregar o relatório do escopo contratado.

Os caminhos de comandos acima são relativos à raiz; não há diretório de usuário fixo. O README apresenta os comandos completos. Para repetir a inicialização, manter a mesma seleção. Não usar esse bootstrap na biblioteca operacional original sem planejamento de adoção.

## Emuladores e frontends

O componente `es-de` possui download pinado no registro `components.json`, herdado da receita validada do projeto. `workspace.py fetch --component es-de` mostra o plano; `--apply` adquire e verifica. Não há download por “latest” sem comparar o hash. Se a origem mudou, bloquear, revisar a nova versão e gerar outra release.

Depois do bootstrap, as receitas materializadas estão em `tools/roms/config/portable-runtimes.json`; suas fontes distribuíveis ficam nos templates. O instalador completo já encadeia essas aquisições. Consulte `python3 tools/roms/emutool.py --help` para diagnóstico/instalação explícita. Não instalar tudo por padrão: consumir o que o host já oferece e instalar somente o escopo contratado. Cada versão ainda precisa revisão de direitos, requisitos e aceitação. NX2 não é componente homologado para oferta comercial.

RetroFE é compilado automaticamente da fonte pinada no perfil completo. Temas, fontes e sons não são copiados da instalação pessoal para a release. Incluir esses recursos exige entradas de origem/licença e uma receita reprodutível. Até isso existir, o adaptador fica disponível como ferramenta e a experiência completa fica pendente.

## Aquisição e organização de mídia

A infraestrutura de download aceita um lock de origem por cliente com `workspace.py fetch --component <id> --source-lock <arquivo.json>`. Use o [exemplo](../../tools/distribution/examples/media-source-lock.json): substituir URL, licença, página de origem, tamanho e SHA-256 por evidências reais, revisar `approved_for_download` e só então aplicar. O exemplo é deliberadamente bloqueado. Não há endpoint genérico que procure imagens sem atribuição.

Mídias usam `media/<system>/<categoria>/<nome-do-jogo>.<ext>`. Associar pelo nome exato e pelos identificadores conhecidos. Atualizar metadados/aliases via as ferramentas existentes; não criar payload duplicado para ES-DE e RetroFE. Mídia baixada com hash correto ainda pode ser arte do jogo errado; revisar essa associação.

Para scraping, reutilizar `romtool media session` e os fluxos ES-DE/Skraper já disponíveis. Credenciais ficam locais, nunca no lock ou repositório. A sessão mantém locks, proteção de gamelist e ingestão de metadados/histórias. Serviços externos, suas contas e a autorização de uso das imagens pertencem ao cliente/contrato; esta prévia não inclui conta, assinatura ou catálogo de mídia licenciado.

## Incidentes e restauração

No setup, arquivo divergente fica intacto e a ação bloqueia. Temporários de download desta tentativa são removidos; o destino só aparece depois do hash completo. Uma inicialização interrompida antes da importação pode ser repetida. Não há comando de remoção automática da biblioteca.

Para mudanças de conteúdo, usar o rollback do manifesto da ferramenta responsável, conferindo hashes e alterações posteriores. Atualizações de código são entregues como release nova em staging; não extrair por cima de userdata. Registrar componente, versão, erro, log sem dados privados e ação corretiva. A coleta de diagnóstico enviada ao suporte deve ser revisada pelo cliente.
