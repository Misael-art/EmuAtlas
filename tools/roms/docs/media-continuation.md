# Continuidade da organização de mídia

## Entrada nativa dos frontends

RetroFE e ES-DE abrem com teclado e gamepads nativos por padrão. O helper compartilhado `steam-input.py session-run` propaga `RETROFE_INPUT_BACKEND=native` aos jogos e conserva o roteador de controles existente para emuladores. O launcher RetroFE usa esse mesmo seletor e preserva as preferências de entrada do desktop no modo nativo.

`RETROFE_INPUT_BACKEND=steam` seleciona a integração opcional. Nesse modo continuam os requisitos de conta autenticada, guardião, lease, troca de perfil e restauração. Um token Steam herdado sem seleção explícita conserva a sessão legada. O comando `steam-input.py input-backend` permite consultar a seleção sem abrir aplicativos. Valores desconhecidos são recusados antes da abertura.

O roteador de jogos consulta `absinfo()` para obter o estado corrente dos analógicos, gatilhos e hats a cada lote de eventos. `capabilities()` identifica os códigos suportados e mantém valores capturados na abertura do dispositivo. As regressões usam estados diferentes para essas duas leituras e conferem movimento, direção e retorno do hat ao neutro.

## Avanço de baixo consumo

`romtool.atomic_json` usa temporário exclusivo no diretório de destino, preserva modo e ACL de um arquivo regular existente, sincroniza os bytes e publica com substituição atômica. A ACL é aplicada antes da escrita dos dados temporários. Destinos que são links simbólicos exigem resolução pelo fluxo especializado. Em POSIX, sincroniza também toda a cadeia de diretórios, incluindo diretórios preparados pelo chamador. Erros de sincronização são propagados ao chamador. Uma falha depois da publicação exige consultar o destino e reconciliar o estado observado.

A implementação Windows sincroniza o arquivo e usa a substituição disponível nesse SO. A cobertura dessa escolha usa simulação em teste. A validação nativa de Windows e do volume compartilhado integra o aceite posterior.

Temporários de chamadas sobrepostas têm nomes exclusivos. A coordenação lógica entre escritores continua a cargo dos locks e das precondições de cada operação. O fluxo stray descrito abaixo também sincroniza movimentos de payload, preimagens e checkpoints por item.

O registro de artefatos admite adiar a leitura integral do histórico quando o host estiver ocupado.

```sh
python3 tools/roms/romtool.py record-artifacts \
  --change-type tooling-update \
  --reason 'Avanço revisado sob orçamento reduzido' \
  --artifacts-json '["tools/roms/romtool.py"]' \
  --defer-history-index
```

O manifesto e os eventos são registrados imediatamente. O manifesto identifica `history_index_refresh=deferred`. O índice completo pode ser atualizado em uma janela adequada.

```sh
python3 tools/roms/romtool.py history
```

A opção padrão de `record-artifacts` mantém a reconstrução imediata do índice.

O registro permanece em `recording` até a publicação durável do manifesto e dos diários. Uma falha identifica o ID na mensagem. `record-artifacts --operation-id <id>` retoma esse mesmo registro com os argumentos originais e confere hashes, finalidade e conteúdo do manifesto existente antes da conclusão. Alterações dos artefatos exigem revisão da evidência. Cada retomada fica registrada no diário da mesma operação.

No Linux, a substituição preserva as ACLs de acesso POSIX/NFSv4 suportadas pelo filesystem e remove ACLs herdadas do temporário quando o arquivo original carece delas. No Windows, o escritor copia a DACL e sua política de herança para o temporário, confere os bytes da DACL e recusa divergências antes da escrita. As chamadas Windows têm fixtures de contrato; seu aceite nativo integra a validação do volume escolhido. [API de segurança Windows](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getfilesecurityw)

## Testes pequenos

```sh
python3 -B -m unittest -v tools.roms.tests.test_atomic_json
python3 -B -m unittest -v tools.roms.tests.test_media_stray_status
```

As fixtures exercitam ordem de sincronização, permissões, criação de diretórios, falhas antes e depois da publicação, escrita sobreposta, colisão de temporário, nome longo, preservação de temporário legado e registro com índice adiado. Cada caso usa arquivos pequenos em diretório temporário privado. Falhas de energia e a execução nativa Windows precisam de evidência adicional em ambiente apropriado.

## Consulta leve da operação interrompida

```sh
python3 tools/roms/romtool.py media stray status --operation-id <operation-id>
```

A consulta lê o manifesto, os eventos locais e os metadados dos caminhos selecionados. O resultado `consistent_metadata` usa código de saída 0. O resultado `reconciliation_required` usa código 3 e inclui os motivos na saída JSON. A evidência identifica `payload_hashes_checked=false` e `global_records_checked=false`, pois essas verificações pertencem à reconciliação integral.

O diagnóstico compara a presença esperada de origem, destino de promoção/quarentena e payload canônico, conforme a ação registrada. Também identifica eventos ilegíveis, registros de outra operação, links ancestrais e caminhos fora do escopo. A observação descreve aquele instante, e mudanças concorrentes exigem nova conferência.

`media stray apply` consulta o diário antes de calcular hashes, criar preimagens ou alterar o manifesto. Um registro de conclusão incompatível com o manifesto impede a reaplicação e orienta a consulta de status. A reconciliação precisa preservar os eventos originais e confirmar os bytes e os registros globais. Depois deve ser preparado um plano atual, respeitando os fingerprints das ferramentas e as demais precondições existentes.

## Checkpoints de movimentos e reversão

O fluxo stray grava a intenção de cada movimento antes do rename, sincroniza os diretórios envolvidos e persiste o resultado de cada item. Uma retomada aceita um destino já presente somente quando a origem está ausente, a intenção registrada coincide com ambos os caminhos e o SHA-256 do destino corresponde ao plano. A conclusão depende da verificação integral dos itens e da publicação durável dos registros correntes.

As preimagens de catálogo, pendências e estado usam cópias CoW quando disponíveis, com cópia de metadados como alternativa nos sistemas de arquivos que exigirem esse procedimento. São registradas assinaturas anteriores e posteriores. A reversão confere essas assinaturas antes de mover payloads e preserva alterações posteriores para reconciliação. Operações legadas que carecem de pós-imagens suficientes exigem reconstruir essa evidência antes de restaurar metadados globais.

Apply e rollback mantêm checkpoints recuperáveis entre o rename e a atualização do item. A publicação repetida dos registros de uma mesma operação mantém uma única entrada corrente de catálogo para cada origem selecionada.

O executor Linux `emutool library` também persiste intenção, identidade do filesystem/inode, permissões e checkpoints de aplicação/reversão. A publicação usa `renameat2` com `RENAME_NOREPLACE` e sincroniza os diretórios envolvidos. Uma colisão posterior à revisão preserva o destino existente. Diretórios somente leitura recebem temporariamente escrita do proprietário para atualizar seu pai, com o modo original registrado e restaurado na retomada. [Documentação de rename](https://man7.org/linux/man-pages/man2/rename.2.html)

Importações selecionadas usam `operations/<id>/staging/library-copies/`, verificam os inventários de fonte e staging e sincronizam os arquivos antes da publicação. Cópias parciais permanecem em `rollback/interrupted-copies/`. A reversão preserva importações publicadas em `rollback/imported/` e arquivos de configuração criados em `rollback/applied-files/`. Os testes pequenos cobrem interrupções entre essas etapas, permissões somente leitura, colisões e mudanças posteriores. A execução nativa em Windows permanece pendente.

A projeção `romtool project-catalog plan|apply|verify|rollback` mantém o diário de origem íntegro e fixa seu SHA-256. Apply e rollback usam o lock compartilhado, preimagens e propostas duráveis, validação integral dos destinos e precondições para o lote inteiro de registros. A publicação interrompida retoma pelos hashes anterior/posterior e mantém as preimagens depois da reversão. Alterações posteriores de conteúdo ou permissões e caminhos com ancestral simbólico exigem reconciliação antes de gravar. A projeção documenta os destinos verificados e conserva a classificação de cada item.

O registry de variantes usa o mesmo escritor durável e o lock compartilhado. A publicação registra intenção antes de gravar, e apply/rollback retomam a partir dos hashes observados. A reversão de um registry criado preserva seus bytes na operação, com retomada entre preservação e retirada do caminho ativo. Mudanças posteriores de conteúdo e permissões permanecem protegidas. Verify exige aplicação ou reversão confirmada no manifesto.

Aliases relativos de lançamento declarados no perfil passam pelo validador compartilhado com o dispatcher nativo. O inventário registra essas relações e seus destinos no diagnóstico de nomes/cobertura. O arquivo simbólico fica fora dos candidatos físicos do registry. Outros links continuam sujeitos às recusas existentes. O diagnóstico comprova o contrato da entrada observado e preserva os limites de identidade, integridade e funcionamento do conjunto.

## Sequência da próxima tarefa

1. Ler as instruções do projeto e a evidência local da interrupção. Medir recursos e identificar outras cargas antes de executar verificações ou aplicar planos.
2. Reconciliar origem, destino, quarentena, SHA-256, manifesto, eventos, catálogo e pendências de cada item selecionado. Registrar a recuperação preservando a evidência histórica.
3. Reforçar os fluxos de movimentação existentes com preimagens duráveis, checkpoints por item e retomada testada. Verificar o resultado antes de concluir a operação.
4. Derivar as entradas reais de lançamento e suas dependências a partir do catálogo, runtime e descritores CUE/GDI/M3U. Reutilizar essa resolução na seleção de scraping, associação de mídia e conversões.
5. Confirmar identidade e ordem dos discos e suporte do runtime antes de propor M3U relativo. Coordenar coleções, gamelists, saves e sidecars no plano reversível.
6. Ajustar ES-DE e Skraper para selecionar as entradas de apresentação previstas. Classificar as trilhas dependentes no conjunto do descritor proprietário.
7. Associar mídia geral ao basename da playlist quando ela for a entrada de apresentação. Preservar arte específica dos discos e alternativas. Compartilhar bytes idênticos apenas depois de comparação integral e prova da associação.
8. Integrar compressão pelos perfis lossless aprovados, com verificação lógica e atualização coordenada dos descritores e referências.
9. Concluir os planos preparados de frontends e assets no escopo revisado. Manter o catálogo e a lista global únicos.
10. Comprovar caminhos, aliases e lançamentos no sistema de arquivos de destino, em Linux e Windows. Registrar navegação, mídia, troca de disco, saves e retorno por combinação testada.

## Raízes compartilhadas

- Jogos e descritores usam `roms/<system_id>/`.
- Mídia de jogo usa `media/<system_id>/<media_type>/`.
- Assets criativos usam `frontend/RetroFE/Assets/shared/` e `frontend/RetroFE/Assets/source-artwork/`, compartilhados com consumidores compatíveis em toda a estrutura.
- Dados compartilhados de emuladores usam `emulator/data/`, com binários nas raízes próprias de Linux e Windows.

Referências configuradas e links relativos precisam funcionar no volume escolhido. Toda mudança deve respeitar os planos especializados, a integridade das dependências e o rollback existente.

## Entradas e trilhas dependentes

A associação de mídia em sessões gerenciadas e a promoção stray compartilham o índice de entradas físicas filtrado pelas extensões do catálogo. Descritores CUE, GDI e M3U válidos fornecem as relações exatas com trilhas existentes dentro da plataforma. Essas trilhas saem dos candidatos de scraping, inclusive FILE WAVE, extensões em maiúsculas e arquivos em subpastas. Um BIN independente continua elegível segundo o catálogo.

Arquivos distintos com o mesmo basename mantêm a ambiguidade, inclusive ZIP e CHD. Uma mídia já existente também passa pela verificação da entrada antes de receber um novo resultado de scraping. O merge de gamelists e a publicação de histórias ignoram trilhas declaradas, preservam registros pessoais existentes e registram as dependências recusadas. A projeção HyperList também exclui essas trilhas de novas publicações.

Esse filtro protege a publicação pelos fluxos gerenciados. A seleção nativa de cada scraper e a apresentação dos frontends dependem das respectivas configurações e planos revisados. Um descritor com referências ausentes conserva seus candidatos para revisão. A resolução de dependências verifica caminhos e relações, enquanto a integridade integral e o funcionamento continuam sujeitos aos validadores e testes próprios.

M3U é a extensão de playlist de múltiplos discos usada pelos runtimes compatíveis. A criação depende de identidade e ordem comprovadas, suporte do runtime e coordenação das referências. Os arquivos usam caminhos relativos aos discos e permanecem junto do conjunto.

## Playlists de conjuntos selecionados

`romtool discs plan --file <pedido.json>` gera a playlist a partir dos discos explicitamente identificados e numerados no pedido. O documento declara `system_id`, `playlist`, `disc_count`, `identity_evidence`, `runtime_evidence` e uma lista `discs` com `path`, `disc_number` e `identity_evidence` por membro. Os caminhos são relativos à pasta da plataforma. A ferramenta confere o catálogo e as capacidades declaradas do runtime, recusa nomes incompatíveis com Windows, arquivos repetidos, trilhas dependentes e descritores incompletos, e registra os SHA-256 integrais dos discos e das dependências CUE/GDI. Archives exigem suporte explícito ao formato dentro da playlist, conforme a seção de disquetes ZIP.

A aplicação exige `romtool discs apply --operation-id <id> --apply`. A publicação mantém os discos nos caminhos existentes, cria o M3U sem sobrescrever arquivos e sincroniza os bytes e o diretório. A retomada confirma o payload e os arquivos do plano. `romtool discs verify --operation-id <id>` revalida hashes e referências. `romtool discs rollback --operation-id <id> --rollback` preserva a playlist em `rollback-playlist.m3u` na operação, com checkpoint de reversão recuperável.

O plano registra a evidência de identidade fornecida na revisão. Hashes e referências confirmam a seleção e sua estabilidade. O aceite funcional exige lançamento, troca de disco, saves e retorno observados no runtime. A publicação do descritor conserva as configurações de frontend e os cartões de memória, que seguem seus próprios planos.

Uma playlist M3U local com referências válidas passa a ser a entrada de associação de mídia e metadados. Seus discos e trilhas ficam classificados como dependências nos fluxos gerenciados. Playlists recursivas e relações que escapam da plataforma permanecem para revisão.

## Projeção das entradas nos frontends

O materializador existente oferece `entries-plan --system-id <id> --manifest <arquivo>`, seguido de `apply --manifest <arquivo> --apply`, `verify` e `rollback --rollback`. O plano usa relações locais CUE/GDI/M3U, registra os descritores e os metadados físicos dos arquivos, preserva preimagens de configuração e confere as coleções que apontam à plataforma canônica.

No ES-DE, dependências recebem `hidden`, `nomultiscrape` e `nogamecount`. O plano define `ShowHiddenGames=false` e `ScraperRespectExclusions=true` para a apresentação e o scraping respeitarem esses campos. O formato fragmentado nativo de es_settings.xml é preservado pelo helper existente, inclusive comentários. Novas entradas M3U reutilizam somente os campos descritivos do primeiro disco. Favoritos e estatísticas permanecem nos registros originais.

No RetroFE, as dependências entram em `exclude.txt` e `exclude_all.txt` nas coleções cuja raiz física foi comprovada. Um basename compartilhado com uma entrada independente fica preservado e consta no resumo. O launcher precisa selecionar a extensão do descritor nesse caso. O plano preserva as extensões configuradas, as ROMs, os arquivos de mídia e os dados de usuário. Ambos os frontends precisam estar fechados para aplicar ou reverter.

Quando o perfil Linux declara formatos e suporte a diretórios para a plataforma, a associação de mídia considera essas capacidades junto do catálogo. Pastas contêiner e archives sem suporte no perfil seguem como dependências ou fontes de revisão. Instalações anteriores sem esse perfil conservam a resolução legada.

## Origens achatadas e reversão parcial

O plano stray admite `--pdf-stream-eol-review` para um PDF selecionado cujo único aviso do qpdf seja `stream keyword followed by carriage return only`. A revisão exige código 3 do qpdf, identificação exata do arquivo em cada aviso e render de todas as páginas pelo Poppler. Outros avisos, erros e páginas ausentes bloqueiam a promoção. O manifesto conserva os avisos completos e o resultado da leitura, e os bytes originais seguem preservados pelo fluxo reversível. Essa seleção é independente da revisão de hints de linearização.

Novos planos stray preservam o caminho físico relativo de cada origem em `rollback/originals/`. Fontes aninhadas e fontes na raiz podem compartilhar o mesmo basename canônico e continuam com destinos de rollback distintos. Os planos antigos conservam seu layout registrado.

Uma aplicação parcial pode ser revertida quando os movimentos concluídos têm checkpoints e os metadados globais ainda coincidem integralmente com as preimagens. A reversão registra essas assinaturas esperadas, preserva cada payload e admite retomada. Movimentos sem checkpoint e alterações posteriores dos registros globais exigem reconciliação antes da reversão.

## Persistência e política do catálogo de assets

`romtool.atomic_write_bytes` compartilha com `atomic_json` a publicação durável de bytes, preservação de permissões e sincronização POSIX. Os materializadores de assets e caminhos de mídia reutilizam esse escritor para seus manifestos e metadados.

O organizador de assets oferece `plan --refresh-catalog-policy --manifest <arquivo> --reason <evidência>`, seguido de apply/verify/rollback. A atualização reconcilia as raízes e o compartilhamento por toda a estrutura e projeta a contagem corrente do catálogo no mapa de mídia. Verifica a existência, o tipo físico e o tamanho dos payloads catalogados. Os SHA-256 dos metadados e do índice protegem a publicação e a reversão. A operação registra seu escopo de conferência, conserva o índice e admite retomar uma publicação parcial dos dois metadados.

## Disquetes ZIP e capacidade do runtime

A criação de playlists confere as capacidades declaradas do perfil da plataforma e registra essas capacidades para a aplicação. Uma alteração relevante de formato ou runtime exige um plano atual. Plataformas configuradas precisam admitir M3U para publicar o conjunto.

PUAE e PUAE2021 permitem referências ZIP na playlist quando o perfil declara os formatos envolvidos. O planejador verifica todos os CRCs, registra o SHA-256 da imagem interna e exige uma única imagem de disquete não vazia em cada ZIP selecionado. Documentação auxiliar permanece no archive. Limites de expansão, quantidade de membros, nomes portáteis, criptografia e links são conferidos antes da leitura integral. Esses checks comprovam a seleção e a integridade do contêiner. A identidade do jogo e o aceite funcional continuam vinculados às evidências da revisão e aos testes nativos. [Documentação PUAE](https://docs.libretro.com/library/puae/)

O plano recusa membros com nomes duplicados ou equivalentes sem distinção de maiúsculas e confere a estabilidade do ZIP até o registro do hash físico. O suporte ZIP precisa pertencer ao mesmo runtime que admite M3U. Os discos originais e a documentação interna permanecem preservados.

## Aliases de categorias ES-DE

Categorias canônicas criadas por uma promoção podem exigir novos aliases do ES-DE. Executar `romtool media esde validate` após o lote identifica essa necessidade. O fluxo `media esde aliases plan|apply|verify|rollback` cria os links relativos para as categorias físicas existentes e registra cada alteração.

A criação registra uma intenção durável antes do link e sincroniza seu diretório em POSIX antes do checkpoint. A retomada de `applying` aceita somente o link que corresponde à intenção registrada. A reversão confere a propriedade dos links, registra a intenção antes de remover cada alias e admite retomar após uma interrupção. Links ocupados por mudanças externas permanecem protegidos pelas precondições.
