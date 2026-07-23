# Good and Bad Tests

Examples use Pester (PowerShell); the principles are framework-agnostic.

## Good Tests

**Integration-style**: Test through real interfaces, not mocks of internal parts.

```powershell
# GOOD: Tests observable behavior
It 'user can checkout with valid cart' {
    $cart = New-Cart
    $cart.Add($product)
    $result = Invoke-Checkout -Cart $cart -PaymentMethod $paymentMethod
    $result.Status | Should -Be 'Confirmed'
}
```

Characteristics:

- Tests behavior users/callers care about
- Uses public API only
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

## Bad Tests

**Implementation-detail tests**: Coupled to internal structure.

```powershell
# BAD: Tests implementation details
It 'checkout calls the payment service' {
    Mock Invoke-PaymentProcess { }
    Invoke-Checkout -Cart $cart -PaymentMethod $payment
    Should -Invoke Invoke-PaymentProcess -ParameterFilter { $Amount -eq $cart.Total }
}
```

Red flags:

- Mocking internal collaborators
- Testing private functions
- Asserting on call counts/order
- Test breaks when refactoring without behavior change
- Test name describes HOW not WHAT
- Verifying through external means instead of the interface

```powershell
# BAD: Bypasses interface to verify
It 'New-User saves to database' {
    New-User -Name 'Alice'
    $row = Invoke-SqlQuery 'SELECT * FROM users WHERE name = @name' @{ name = 'Alice' }
    $row | Should -Not -BeNullOrEmpty
}

# GOOD: Verifies through interface
It 'New-User makes user retrievable' {
    $user = New-User -Name 'Alice'
    (Get-User -Id $user.Id).Name | Should -Be 'Alice'
}
```

**Tautological tests**: Expected value restates the implementation, so the test passes by construction.

```powershell
# BAD: Expected value is recomputed the way the code computes it
It 'Get-CartTotal sums line items' {
    $items = @(@{ Price = 10 }, @{ Price = 5 })
    $expected = ($items | Measure-Object -Property Price -Sum).Sum
    Get-CartTotal $items | Should -Be $expected
}

# GOOD: Expected value is an independent, known literal
It 'Get-CartTotal sums line items' {
    Get-CartTotal @(@{ Price = 10 }, @{ Price = 5 }) | Should -Be 15
}
```

<!-- Adapted from mattpocock/skills (MIT); see skills/NOTICE.md -->
