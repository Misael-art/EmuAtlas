# Qualidade, suporte e evolução

## Estados verificáveis

`catalogado` → `receita disponível` → `adquirido e íntegro` → `host compatível` → `frontend integrado` → `gameplay aceito` → `suporte contratado`.

Cada transição pede evidência própria. Não pular de checksum correto para “funciona”. Um teste automatizado não aprova controles físicos ou retorno ao desktop.

## Matriz inicial

| Área | Situação desta prévia | Gate para oferta comercial |
| --- | --- | --- |
| Estrutura/configuração vazia | Implementada e submetida a teste de repetição/realocação | Repetir em host limpo contratado |
| Exportação do pacote | Allowlist, SHA-256 e arquivo determinístico | Revisão do diff, integridade e licença da seleção |
| Instalador de host | Grafo de 59 componentes, pacotes/Flatpak/fontes/configuração e retomada | Aceitação em hosts limpos por distribuição |
| Ferramentas de organização | Código existente reutilizado | Insumos e regressões do escopo do cliente |
| Mídias | Download de itens pinados + fluxos de ingestão existentes | Provedor/licença configurados e associação revisada |
| RetroFE e temas | Receita de compilação e fallback textual; temas pessoais não empacotados | Build em host limpo, direitos, testes visuais e navegação |
| Desktop/Steam Deck | Proteções existentes mantidas | Teste físico: entrar, jogar, sair/crash e recuperar atalhos |
| Windows/macOS/ARM | Não homologados | Portar dependências, paths e controles antes de vender suporte |
| Switch 2 / Eden-NX2 | Experimental, gameplay não comprovado | Build compatível e aceitação física; fora da oferta inicial |

## Desenvolvimento e entrega

- 0.1: base privada, produto documentado, instalação vazia, empacotamento e CI de distribuição.
- 0.2: instalador do ecossistema implementado; testar instalação completa em máquinas limpas.
- Próxima entrega: fechar origem/licenças e primeiro tema aprovado, wheelhouse/versões por host e atualização em staging.
- Piloto pago delimitado: uma máquina, frontend e plataformas com evidências, sem prometer cobertura total.
- Versão comercial: instalação e atualização em staging, rollback testado, suporte mensurável e materiais aprovados.

Antes de entregar uma máquina: verificar espaço/dependências, origem dos conteúdos, descoberta da biblioteca, A1→A2→jogo→retorno, Options/Exit em todo tema suportado, resolução/monitor, dispositivo desconectado, encerramento normal/crash e restauração do desktop. Registrar tempo de inicialização/navegação e problemas observados, sem inventar FPS ou latência.

O suporte recebe versão do pacote, componente, host e reprodução mínima. Conteúdos pessoais não devem ser requisito para reportar um bug de instalador. Definir prazos de atendimento, janela de atualização e exclusões em cada contrato; esta documentação não cria um SLA ou canal de suporte ativo.
