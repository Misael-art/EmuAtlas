# Jornada de saves do RetroArch

A sessão gerenciada acompanha as gravações do jogo lançado pelo EmuAtlas. Cada versão recebe sequência, data, horário, identidade do conteúdo, hash, tempo observado e uma imagem quando o core a disponibiliza. O monitor começa junto do processo do RetroArch e encerra com ele.

## Usar durante o jogo

| Ação | Mouse e teclado | Gamepad P1, nomes lógicos SDL |
| --- | --- | --- |
| Abrir as gravações | Passe o mouse sobre o cartão esquerdo ou Ctrl + Shift + J | Select + Y |
| Percorrer | Roda do mouse ou ↑/↓ | Direcional ou analógico esquerdo |
| Retomar | Clique no cartão ou Enter | A |
| Favoritar | Coração ou F | X |
| Editar marcações | ••• | Start |
| Fechar | × ou Esc | B |
| Exportar planilha | CSV no rodapé | Use o mouse |

O cartão compacto destaca um favorito, ou a gravação mais recente. A lista expandida mostra cinco cartões e oferece rolagem. Favoritos vêm primeiro, ordenados do mais recente ao mais antigo dentro do grupo. As demais gravações seguem a mesma ordem temporal. A numeração representa a sequência de versões observadas daquele jogo/core.

As marcações incluem Chefe, Final, Hiperdesafio, Descoberta, Marco, Treino e Fase 1–9999. O editor aceita uma nota de até 500 caracteres. O jogo pausa ao abrir a lista. Fechar libera somente a pausa assumida pelo monitor. Uma pausa previamente aplicada pelo usuário continua sob seu controle ao fechar a lista. Escolher Retomar solicita explicitamente a continuação do jogo. Enquanto a lista captura a entrada, o roteador neutraliza as portas do jogo e exige soltura dos comandos ao retornar.

A interface usa grafite, dourado discreto, contraste de texto e animação curta. O cartão ocupa a lateral esquerda, dimensionada pela janela e pela proporção da captura. Em janelas sem margem estimada, uma pequena entrada Saves abre a lista. A lista expandida pode cobrir parte do jogo durante a pausa. `emuatlas_reduce_motion = "true"` na configuração RetroArch reduz as animações.

## Armazenamento portátil

```text
emulator/data/RetroArch/
├── saves/                          # Saves de bateria correntes do core
├── states/                         # States correntes e slots progressivos
│   └── journey/<game_id>/
│       ├── history.sqlite3         # Sequência, relações, favoritos, tags e sessões
│       ├── monitor.lock            # Exclusão entre monitores do mesmo jogo/core
│       ├── objects/<sha256>.*      # Versões dos bytes nativos
│       ├── previews/00000001.png   # Imagem e metadados imutáveis EmuAtlas
│       └── sessions/<operation>/session.cfg
└── screenshots/                    # Galeria nativa configurada
outputs/.../operations/<session>/    # Diagnóstico, resumo e exportações CSV
```

O identificador é derivado da plataforma, caminho relativo da entrada de lançamento e nome do core. Extensões `.so` e `.dll` convergem para a mesma identidade. Os registros internos usam caminhos relativos à instalação. O launcher recalcula os caminhos de execução ao abrir em outra raiz.

SQLite mantém os dados mutáveis e as relações. Cada PNG incorpora um bloco textual comprimido `EmuAtlas`, com os dados imutáveis conhecidos na captura. Os arquivos nativos preservados mantêm seus bytes originais e são compartilhados por SHA-256 quando idênticos dentro do mesmo jogo/core e função. Saves de bateria podem incluir o `.rtc` adjacente como dependência. Favoritos, tags e notas são consultados no SQLite.

A exportação CSV UTF-8 com BOM abre em planilhas. Inclui sequência, data, tipo, tempos, favorito, tags, fase, nota, origem, caminho preservado, imagem, hash, origem da medição e dependências. Campos textuais recebem proteção contra interpretação como fórmula. CSV é uma projeção regenerável. A fonte de consulta é o SQLite.

```bash
python3 tools/roms/save_journey.py --system-id sms \
  --rom 'roms/sms/Meu Jogo.sms' --core genesis_plus_gx_libretro \
  --export-csv outputs/minha-jornada.csv
```

## Captura e fidelidade dos dados

O monitor usa o stdin privado do processo filho. O perfil efêmero habilita comandos, thumbnails, slots progressivos, ausência de poda e flush de bateria a cada cinco segundos. `config_save_on_exit=false` mantém essas opções restritas à sessão. A configuração de screenshots e os atalhos nativos continuam disponíveis pela configuração vigente.

As pastas de saves/states são observadas a cada 750 ms, com estabilização e verificação integral do arquivo. O monitor reconhece os nomes exatos do conteúdo e os caminhos declarados pelo RetroArch dentro das raízes canônicas. Arquivos existentes são importados gradualmente. A troca de conteúdo pelo menu interno suspende a captura da entrada original.

| Informação | Evidência e limite |
| --- | --- |
| Data e hora do save | mtime do arquivo, com fuso horário registrado |
| Número e anterior | Sequência transacional do histórico daquele jogo/core |
| Tempo de sessão e acumulado | Intervalos observados com GET_STATUS PLAYING, desde o início do monitoramento |
| Imagem do state | Thumbnail nativo associado à versão, completamente decodificado |
| Imagem do save de bateria | Thumbnail do state acompanhante, com diferença temporal registrada, ou screenshot posterior à gravação |
| Identidade | Caminho original, conteúdo lançado, hash do core e SHA-256 do conteúdo até 64 MiB, ou CRC reportado e assinatura do arquivo |
| Arquivos antigos | Tempo anterior e core de criação desconhecidos, explicitados no registro |
| Fase, chefe e progresso | Classificação manual do usuário |

O salvamento interno só fica visível ao monitor quando o core o grava em disco. Várias ações internas entre dois flushes podem produzir uma única versão. Regravações de bytes idênticos no mesmo caminho conservam uma versão. O tempo medido representa execução observada, com precisão de amostragem, e o histórico anterior permanece desconhecido.

Para um save de bateria, cores com serialização podem gerar um state acompanhante. A relação registra o atraso e compartilha a imagem. Cores sem serialização conservam o save, com carregamento pelo menu nativo do jogo. A retomada visual de bateria usa seu state acompanhante. O menu informa a indisponibilidade quando esse state falta.

RASTATE v1 é validado por estrutura, memória e terminador. Contêineres RZIP v1 passam por descompressão limitada, tamanho e checksum integral. Formatos legados específicos de core recebem classificação explícita de formato opaco. A aceitação de um state pelo jogo depende também da compatibilidade do core.

## Retomada e recuperação

1. Conferir conteúdo ativo, identidade, hash do core, hash da versão e espaço livre.
2. Solicitar e preservar um novo state do momento corrente.
3. Copiar a versão selecionada para um slot nativo temporário alto, depois da cópia de segurança.
4. Registrar a intenção e publicar sem sobrescrita, com hash verificado e sincronização em disco.
5. Solicitar LOAD_STATE_SLOT e aguardar callback nativo, ausência de erro e status posterior.
6. Solicitar continuação atrás da imagem centralizada, aguardar PLAYING em dois ciclos posteriores e fazer a transição suave para os frames restaurados.
7. Limpar apenas a cópia derivada de carregamento sob assinatura e hash correspondentes.

Uma recusa tenta recuperar o momento anterior e informa o resultado. Uma solicitação nativa já enviada continua aguardando confirmação, pois a API não fornece cancelamento seguro. Após interrupção, a próxima sessão reconcilia cópias de carregamento registradas. Arquivos divergentes são preservados. Versões históricas permanecem disponíveis.

A publicação do payload antecede a transação do catálogo. Uma interrupção pode deixar um objeto sem referência, preservado para revisão. SQLite usa transações duráveis. Existe um lease por jogo/core. O arquivo de configuração e o diário pertencem à operação de sessão. O monitor limita payloads a 512 MiB, imagens a 16.777.216 pixels e exige reserva de 10 GiB além do trabalho estimado. A retenção do histórico é cumulativa e o tamanho de cada versão acompanha o core.

## Escopo de aceitação

A integração visual usa o roteador Linux/KDE existente. O caminho sem esse roteador registra o histórico e conserva o carregamento nativo do RetroArch. O backend contém tratamento de locks e publicação para Windows. Aceitação Windows, foco/composição na sessão física e gameplay com controles reais exigem ensaio no host correspondente.

O ensaio automatizado local usa RetroArch 1.22.2 instalado, core libretro próprio, conteúdo sintético, vídeo nulo e UI em Xvfb. O teste verifica bytes serializados, SRAM, thumbnail, retomada do contador original e primeiro frame posterior. As capturas do ensaio mostram a interface com conteúdo sintético. As matrizes locais de telas cobrem 16:10, 16:9, 4:3, 5:4, ultrawide e retrato.

## Referências upstream

- RetroArch command interface e comandos de sessão. [Documentação oficial](https://docs.libretro.com/development/retroarch/network-control-interface/)
- Implementação de comandos na versão ensaiada. [command.c](https://github.com/libretro/RetroArch/blob/69a4f0ea1e/command.c)
- Serialização, tarefas de save/load e thumbnails. [task_save.c](https://github.com/libretro/RetroArch/blob/69a4f0ea1e/tasks/task_save.c)
- Contêiner de compressão lossless nativo. [rzip_stream.c](https://github.com/libretro/RetroArch/blob/69a4f0ea1e/libretro-common/streams/rzip_stream.c)
